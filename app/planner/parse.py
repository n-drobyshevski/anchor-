"""Parsing `/task` and `/event` (P3): a deterministic regex first, a
strict-schema call on the safety model only when that misses.

Same two-tier shape as app/core/quiet.py's parser (pure, table-driven,
tested without a provider) plus app/core/extract.py's fallback
discipline (strict `json_schema` is a reliability aid only; every field
is re-validated in code regardless of which path produced it -- a
model that can emit a field is a model that can emit the wrong one).

The regex path is free and instant, and handles the phrasing most
`/task` and `/event` messages will actually use: a bare title, an
ISO or `DD.MM[.YYYY]` date, `сегодня`/`завтра`/`послезавтра` /
`today`/`tomorrow`, an explicit `в HH:MM` / `at HH:MM` time for
`/event`, and an optional `на N ч|мин` duration. Anything it cannot
place falls back to the safety model, gated by `check_cap` exactly as
app/core/extract.py gates its own call -- a planner parse is not
exempt from the daily spend cap just because it is short.

`PLANNER_MAX_WRITES_PER_DAY` is **not** enforced here: it counts
planner_action rows (app/planner/actions.py's count_today), and a
parse that never becomes an action (bad input, LLM refusal) should not
spend part of that budget. The command handler (app/tg/planner.py)
checks it right before calling app.planner.actions.create.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock, combine_local, floating_utc_midnight
from app.core.spend import check_cap, priced
from app.db.models import SpendLedger
from app.llm.provider import JSONSchema, LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

__all__ = [
    "ParseError",
    "TaskInput",
    "EventInput",
    "parse_task",
    "parse_event",
]

PLANNER_CATEGORY = "planner"

TITLE_MIN = 1
TITLE_MAX = 200
MAX_FUTURE = datetime.timedelta(days=365)

DEFAULT_EVENT_DURATION = datetime.timedelta(hours=1)


class ParseError(Exception):
    """The input could not become a task/event -- deterministically or
    by the model. `message` is shown to the user as-is, so it is always
    a short Russian sentence, never a repr of the underlying failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class TaskInput:
    title: str
    due_date: datetime.date | None


@dataclass(frozen=True)
class EventInput:
    title: str
    start: datetime.datetime  # aware UTC
    end: datetime.datetime  # aware UTC
    all_day: bool


# --- shared date-phrase vocabulary -------------------------------------

_RELATIVE_DAYS = {
    "сегодня": 0, "today": 0,
    "завтра": 1, "tomorrow": 1,
    "послезавтра": 2,
}
_RELATIVE_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _RELATIVE_DAYS) + r")\b", re.IGNORECASE
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_RU_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")

# A marker word directly before the date token -- "на завтра", "to
# 2026-10-01" -- is consumed along with the token, so it never survives
# into the title (task.py's /task usage text advertises exactly this
# "<название> на <дата>" shape).
_TRAILING_MARKER_RE = re.compile(r"(?:^|\s)(?:на|to|by|due)\s*$", re.IGNORECASE)


class _InvalidDateToken(Exception):
    """A token shaped like a date (`31.02`) does not name a real one."""


def _resolve_date_token(match: re.Match, today: datetime.date) -> datetime.date | None:
    """The date a single matched token names, or None if it does not parse
    (e.g. `31.02`)."""
    text = match.group(0)
    if match.re is _RELATIVE_RE:
        return today + datetime.timedelta(days=_RELATIVE_DAYS[text.lower()])
    if match.re is _ISO_DATE_RE:
        year, month, day = (int(g) for g in match.groups())
    else:  # _RU_DATE_RE
        day, month, year_raw = match.groups()
        day, month = int(day), int(month)
        if year_raw is None:
            year = today.year
        else:
            year = int(year_raw)
            if year < 100:
                year += 2000
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _extract_date(text: str, today: datetime.date) -> tuple[str, datetime.date | None]:
    """The first date token found (relative word, ISO, or DD.MM[.YYYY]),
    removed from `text`. `(text, None)` if none is found at all.

    Raises `_InvalidDateToken` when a token that looks like a date does
    not name a real one (e.g. `31.02`) -- the caller treats that as a
    regex *miss*, not as "no date": the text still needs a date, just
    one this deterministic path cannot read, so it falls back to the
    model instead of silently dropping the token and proceeding
    dateless.
    """
    for regex in (_ISO_DATE_RE, _RU_DATE_RE, _RELATIVE_RE):
        match = regex.search(text)
        if match is None:
            continue
        date = _resolve_date_token(match, today)
        if date is None:
            raise _InvalidDateToken(match.group(0))
        before = text[: match.start()]
        marker = _TRAILING_MARKER_RE.search(before)
        cut_start = marker.start() if marker else match.start()
        remainder = (text[:cut_start] + text[match.end() :]).strip()
        remainder = re.sub(r"\s{2,}", " ", remainder)
        return remainder, date
    return text, None


_TIME_RE = re.compile(
    r"\b(?:в|at)\s+(\d{1,2})[:.](\d{2})\b|\b(\d{1,2}):(\d{2})\b", re.IGNORECASE
)
_DURATION_RE = re.compile(
    r"\bна\s+(\d{1,3})\s*(ч(?:ас\w*)?|мин\w*|h|m)\b", re.IGNORECASE
)
_ALL_DAY_RE = re.compile(r"\b(весь день|целый день|all day)\b", re.IGNORECASE)


def _extract_time(text: str) -> tuple[str, datetime.time | None]:
    match = _TIME_RE.search(text)
    if match is None:
        return text, None
    groups = match.groups()
    hour = int(groups[0] if groups[0] is not None else groups[2])
    minute = int(groups[1] if groups[1] is not None else groups[3])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return text, None
    remainder = (text[: match.start()] + text[match.end() :]).strip()
    remainder = re.sub(r"\s{2,}", " ", remainder)
    return remainder, datetime.time(hour, minute)


def _extract_duration(text: str) -> tuple[str, datetime.timedelta | None]:
    match = _DURATION_RE.search(text)
    if match is None:
        return text, None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    delta = (
        datetime.timedelta(hours=amount)
        if unit.startswith(("ч", "h"))
        else datetime.timedelta(minutes=amount)
    )
    remainder = (text[: match.start()] + text[match.end() :]).strip()
    remainder = re.sub(r"\s{2,}", " ", remainder)
    return remainder, delta


def _clean_title(text: str) -> str | None:
    title = re.sub(r"\s{2,}", " ", text).strip(" ,.-—")
    if not (TITLE_MIN <= len(title) <= TITLE_MAX):
        return None
    return title


def _within_horizon(moment: datetime.datetime, clock: Clock) -> bool:
    return clock.now_utc() <= moment <= clock.now_utc() + MAX_FUTURE


# --- regex parsers -------------------------------------------------------


def _regex_parse_task(text: str, clock: Clock, timezone: str) -> TaskInput | None:
    today = clock_module.local_date(clock, timezone)
    try:
        remainder, due_date = _extract_date(text, today)
    except _InvalidDateToken:
        return None
    title = _clean_title(remainder)
    if title is None:
        return None
    return TaskInput(title=title, due_date=due_date)


def _regex_parse_event(text: str, clock: Clock, timezone: str) -> EventInput | None:
    today = clock_module.local_date(clock, timezone)

    all_day_match = _ALL_DAY_RE.search(text)
    without_all_day = (
        (text[: all_day_match.start()] + text[all_day_match.end() :]).strip()
        if all_day_match
        else text
    )

    try:
        without_date, date = _extract_date(without_all_day, today)
    except _InvalidDateToken:
        return None
    date = date or today

    if all_day_match:
        title = _clean_title(without_date)
        if title is None:
            return None
        start = floating_utc_midnight(date)
        end = floating_utc_midnight(date + datetime.timedelta(days=1))
        return EventInput(title=title, start=start, end=end, all_day=True)

    without_time, time_of_day = _extract_time(without_date)
    if time_of_day is None:
        # No explicit time and no "весь день" marker: too ambiguous for
        # the deterministic path (is it all day, or did the time just
        # get left out?). Let the model ask a real question of the text.
        return None

    without_duration, duration = _extract_duration(without_time)
    duration = duration or DEFAULT_EVENT_DURATION

    title = _clean_title(without_duration)
    if title is None:
        return None

    start = combine_local(date, time_of_day, timezone)
    end = start + duration
    return EventInput(title=title, start=start, end=end, all_day=False)


# --- validation, shared by both paths -------------------------------------


def _validate_task(title, due_date) -> TaskInput:
    clean = _clean_title(title) if isinstance(title, str) else None
    if clean is None:
        raise ParseError("Не поняла название задачи.")
    if due_date is not None and not isinstance(due_date, datetime.date):
        raise ParseError("Не поняла дату.")
    return TaskInput(title=clean, due_date=due_date)


def _validate_event(title, start, end, all_day, *, clock: Clock) -> EventInput:
    clean = _clean_title(title) if isinstance(title, str) else None
    if clean is None:
        raise ParseError("Не поняла название события.")
    if not isinstance(start, datetime.datetime) or not isinstance(end, datetime.datetime):
        raise ParseError("Не поняла время.")
    if end <= start:
        raise ParseError("Событие не может кончаться раньше, чем начинается.")
    if not _within_horizon(start, clock):
        raise ParseError("Слишком далеко: планирую максимум на год вперёд.")
    return EventInput(title=clean, start=start, end=end, all_day=bool(all_day))


# --- the safety-model fallback --------------------------------------------

TASK_SCHEMA = JSONSchema(
    name="anchor_planner_task",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "due_date"],
        "properties": {
            "title": {"type": "string"},
            "due_date": {"type": ["string", "null"], "description": "yyyy-MM-dd or null"},
        },
    },
)

EVENT_SCHEMA = JSONSchema(
    name="anchor_planner_event",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "date", "start_time", "end_time", "all_day"],
        "properties": {
            "title": {"type": "string"},
            "date": {"type": "string", "description": "yyyy-MM-dd, today's date if unstated"},
            "start_time": {"type": ["string", "null"], "description": "HH:MM 24h, or null if all_day"},
            "end_time": {"type": ["string", "null"], "description": "HH:MM 24h, or null if all_day"},
            "all_day": {"type": "boolean"},
        },
    },
)

_TASK_PROMPT = (
    "Пользователь диктует задачу для планера на русском или английском. "
    "Верни JSON по схеме: `title` — короткое название без даты, "
    "`due_date` — срок в формате yyyy-MM-dd, или null, если срока нет. "
    "Сегодня {today}."
)
_EVENT_PROMPT = (
    "Пользователь диктует событие для планера на русском или английском. "
    "Верни JSON по схеме: `title` — короткое название без даты и времени, "
    "`date` в формате yyyy-MM-dd (сегодня, если день не назван), "
    "`start_time`/`end_time` в формате HH:MM (24ч), или null для обоих, "
    "если событие на весь день (тогда `all_day: true`). "
    "Если длительность не названа, `end_time` — через час после `start_time`. "
    "Сегодня {today}."
)


async def _cap_check_or_raise(
    session: AsyncSession, settings: Settings, clock: Clock, timezone: str
) -> None:
    if await check_cap(session, settings, clock, timezone):
        raise ParseError(
            "Сегодняшний лимит на обращения к модели исчерпан — "
            "попробуй сформулировать проще (с датой и временем явно) или завтра."
        )


async def _record_ledger(
    session: AsyncSession, settings: Settings, clock: Clock, timezone: str, response
) -> None:
    cost = priced(response.usage, settings, model=response.model)
    session.add(
        SpendLedger(
            local_date=clock_module.local_date(clock, timezone),
            category=PLANNER_CATEGORY,
            model=response.model,
            tokens_in=response.usage.input_tokens,
            tokens_cached=response.usage.cached_tokens,
            tokens_out=response.usage.output_tokens,
            usd_cost=cost.usd,
            cost_source=cost.source,
        )
    )
    await session.commit()


def _parse_json_object(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    candidate = match.group() if match else raw
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _llm_parse_task(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    clock: Clock,
    *,
    text: str,
    timezone: str,
) -> TaskInput:
    await _cap_check_or_raise(session, settings, clock, timezone)
    today = clock_module.local_date(clock, timezone)
    response = await provider.complete(
        [
            LLMMessage(role="system", content=_TASK_PROMPT.format(today=today.isoformat())),
            LLMMessage(role="user", content=text),
        ],
        conversation_id=f"anchor-planner-task-{clock.now_utc().timestamp()}",
        json_schema=TASK_SCHEMA,
    )
    await _record_ledger(session, settings, clock, timezone, response)

    payload = _parse_json_object(response.text)
    if payload is None:
        raise ParseError("Не поняла задачу. Попробуй ещё раз, с датой явно.")

    due_date = None
    raw_due = payload.get("due_date")
    if isinstance(raw_due, str) and raw_due:
        try:
            due_date = datetime.date.fromisoformat(raw_due)
        except ValueError:
            raise ParseError("Не поняла дату.") from None

    return _validate_task(payload.get("title"), due_date)


async def _llm_parse_event(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    clock: Clock,
    *,
    text: str,
    timezone: str,
) -> EventInput:
    await _cap_check_or_raise(session, settings, clock, timezone)
    today = clock_module.local_date(clock, timezone)
    response = await provider.complete(
        [
            LLMMessage(role="system", content=_EVENT_PROMPT.format(today=today.isoformat())),
            LLMMessage(role="user", content=text),
        ],
        conversation_id=f"anchor-planner-event-{clock.now_utc().timestamp()}",
        json_schema=EVENT_SCHEMA,
    )
    await _record_ledger(session, settings, clock, timezone, response)

    payload = _parse_json_object(response.text)
    if payload is None:
        raise ParseError("Не поняла событие. Попробуй ещё раз, с датой и временем явно.")

    try:
        date = datetime.date.fromisoformat(payload.get("date", ""))
    except ValueError:
        raise ParseError("Не поняла дату.") from None

    all_day = bool(payload.get("all_day"))
    if all_day:
        start = floating_utc_midnight(date)
        end = floating_utc_midnight(date + datetime.timedelta(days=1))
    else:
        try:
            start_h, start_m = (int(x) for x in (payload.get("start_time") or "").split(":"))
            end_h, end_m = (int(x) for x in (payload.get("end_time") or "").split(":"))
        except (ValueError, AttributeError):
            raise ParseError("Не поняла время.") from None
        start = combine_local(date, datetime.time(start_h, start_m), timezone)
        end = combine_local(date, datetime.time(end_h, end_m), timezone)
        # No "crossed midnight" leniency here: _validate_event's plain
        # `end <= start` check is what the contract asks for, and an
        # overnight event is exactly the case the model should have named
        # with tomorrow's date in `date` -- not a case for this parser to
        # guess at silently.

    return _validate_event(payload.get("title"), start, end, all_day, clock=clock)


# --- public entry points ---------------------------------------------------


async def parse_task(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    clock: Clock,
    *,
    text: str,
    timezone: str,
) -> TaskInput:
    """`text` is the raw `/task` argument string. Raises ParseError."""
    text = (text or "").strip()
    if not text:
        raise ParseError("Что за задача? /task <название> [на <дата>].")
    result = _regex_parse_task(text, clock, timezone)
    if result is not None:
        return _validate_task(result.title, result.due_date)
    return await _llm_parse_task(session, settings, provider, clock, text=text, timezone=timezone)


async def parse_event(
    session: AsyncSession,
    settings: Settings,
    provider: LLMProvider,
    clock: Clock,
    *,
    text: str,
    timezone: str,
) -> EventInput:
    """`text` is the raw `/event` argument string. Raises ParseError."""
    text = (text or "").strip()
    if not text:
        raise ParseError("Что за событие? /event <название> в <ЧЧ:ММ> [дата].")
    result = _regex_parse_event(text, clock, timezone)
    if result is not None:
        return _validate_event(result.title, result.start, result.end, result.all_day, clock=clock)
    return await _llm_parse_event(session, settings, provider, clock, text=text, timezone=timezone)
