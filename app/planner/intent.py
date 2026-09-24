"""Chat-intent detection for the planner (P4), behind `PLANNER_INTENT`.

A cheap regex prefilter runs on every turn's user text -- almost every
turn misses it, and a miss costs nothing at all, not even a function
call worth mentioning (see `prefilter_hit`). On a hit, `detect()` makes
one strict-`JSONSchema` call on the safety model, run by app/core/turn.py
*inside its existing `asyncio.gather` with welfare.classify* -- not as a
second sequential round trip, and not as its own job. That placement is
the point: it costs the ordinary turn nothing when welfare is already
the slower of the two calls, and it shares welfare's fail-open and
mode-gating discipline for free, because turn.py only starts the gather
at all when `run_welfare` is true (persona mode, not a hard pause).

**This module never writes anything.** `detect()` returns a proposed
`(kind, payload)` or `None`; app/core/turn.py is the one that turns a
result into a `planner_action` row (app/planner/actions.create), which
is exactly the same pending card `/task` and `/event` produce
(app/tg/planner.py's send_confirm_card) -- P4 adds a second *source*
for a card, not a second kind of card. A user still taps [Добавить]
before anything reaches the planner.

Validation reuses app/planner/parse.py's `_validate_task` /
`_validate_event` directly (same title-length, `end > start` and
one-year-horizon rules `/task` and `/event` enforce) rather than
duplicating them -- a chat-inferred date is no more trustworthy than a
`/task`-typed one, so it earns no lighter check.

Fails open like welfare.classify: a timeout, a provider error, or a
reply that does not parse all produce `(None, None)`, not an exception.
An intent that silently failed to detect is an ordinary chat turn; an
intent detector that could crash a turn would be a worse trade than the
feature is worth.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock, combine_local, floating_utc_midnight
from app.llm.provider import JSONSchema, LLMMessage, LLMProvider
from app.planner import actions as planner_actions
from app.planner.parse import (
    EventInput,
    ParseError,
    TaskInput,
    _parse_json_object,
    _validate_event,
    _validate_task,
)

logger = logging.getLogger(__name__)

__all__ = [
    "IntentResult",
    "prefilter_hit",
    "detect",
]

# RU + EN trigger phrases (plan section 4's list, verbatim). A prefilter
# miss is the overwhelmingly common case -- most turns are not about the
# planner at all -- so this is a plain `re.search`, no model involved.
_TRIGGERS = (
    "напомни", "запиши", "добавь в планер", "добавь в календарь",
    "запланируй", "remind me", "add to calendar", "schedule",
)
PREFILTER_RE = re.compile(
    "|".join(re.escape(word) for word in _TRIGGERS), re.IGNORECASE
)


def prefilter_hit(text: str) -> bool:
    return bool(PREFILTER_RE.search(text or ""))


INTENT_SCHEMA = JSONSchema(
    name="anchor_planner_intent",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "title", "due_date", "date", "start_time", "end_time", "all_day"],
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["task", "event", "none"],
                "description": "'none' if this is not actually a request to add "
                "something to the planner",
            },
            "title": {"type": ["string", "null"]},
            "due_date": {
                "type": ["string", "null"],
                "description": "yyyy-MM-dd, task only",
            },
            "date": {
                "type": ["string", "null"],
                "description": "yyyy-MM-dd, event only; today if unstated",
            },
            "start_time": {"type": ["string", "null"], "description": "HH:MM 24h, event only"},
            "end_time": {"type": ["string", "null"], "description": "HH:MM 24h, event only"},
            "all_day": {"type": "boolean"},
        },
    },
)

_INTENT_PROMPT = (
    "Пользователь пишет обычное сообщение в чат, но иногда просит записать "
    "дело в планер (напомни, запиши, добавь в планер/календарь, запланируй "
    "и т.п. по-русски или по-английски). Определи, есть ли в последнем "
    "сообщении именно такая просьба -- а не просто разговор о планах.\n"
    "Если нет -- верни kind: \"none\", остальные поля null/false.\n"
    "Если это задача без точного времени -- kind: \"task\", title -- короткое "
    "название без даты, due_date -- срок yyyy-MM-dd или null, если срока нет.\n"
    "Если это событие с временем или конкретным днём -- kind: \"event\", "
    "title -- короткое название без даты и времени, date в формате yyyy-MM-dd "
    "(сегодня, если день не назван), start_time/end_time в формате HH:MM (24ч), "
    "или null для обоих, если событие на весь день (тогда all_day: true). "
    "Если длительность не названа, end_time -- через час после start_time.\n"
    "Сегодня {today}."
)


class IntentResult:
    """A proposed write, in the same shape app/tg/router.py's `/task` and
    `/event` handlers build for `planner_actions.create()` -- see
    router.py's `_parse` closures for the payload dicts this mirrors."""

    __slots__ = ("kind", "payload", "title")

    def __init__(self, kind: str, payload: dict, title: str) -> None:
        self.kind = kind
        self.payload = payload
        self.title = title


def _task_result(payload: dict) -> IntentResult | None:
    due_raw = payload.get("due_date")
    due_date = None
    if isinstance(due_raw, str) and due_raw:
        try:
            due_date = datetime.date.fromisoformat(due_raw)
        except ValueError:
            return None
    try:
        task: TaskInput = _validate_task(payload.get("title"), due_date)
    except ParseError:
        return None
    return IntentResult(
        planner_actions.CREATE_TASK,
        {"title": task.title, "due_date": task.due_date.isoformat() if task.due_date else None},
        task.title,
    )


def _event_result(payload: dict, timezone: str, clock: Clock) -> IntentResult | None:
    raw_date = payload.get("date")
    if not isinstance(raw_date, str):
        return None
    try:
        date = datetime.date.fromisoformat(raw_date)
    except ValueError:
        return None

    all_day = bool(payload.get("all_day"))
    if all_day:
        start = floating_utc_midnight(date)
        end = floating_utc_midnight(date + datetime.timedelta(days=1))
    else:
        try:
            start_h, start_m = (int(x) for x in (payload.get("start_time") or "").split(":"))
            end_h, end_m = (int(x) for x in (payload.get("end_time") or "").split(":"))
        except (ValueError, AttributeError):
            return None
        start = combine_local(date, datetime.time(start_h, start_m), timezone)
        end = combine_local(date, datetime.time(end_h, end_m), timezone)

    try:
        event: EventInput = _validate_event(
            payload.get("title"), start, end, all_day, clock=clock
        )
    except ParseError:
        return None
    return IntentResult(
        planner_actions.CREATE_EVENT,
        {
            "title": event.title,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "all_day": event.all_day,
        },
        event.title,
    )


def _to_result(raw_text: str, *, clock: Clock, timezone: str) -> IntentResult | None:
    payload = _parse_json_object(raw_text)
    if payload is None:
        return None
    kind = payload.get("kind")
    if kind == "task":
        return _task_result(payload)
    if kind == "event":
        return _event_result(payload, timezone, clock)
    return None  # "none", or anything the schema did not actually constrain


async def detect(
    provider: LLMProvider,
    settings: Settings,
    clock: Clock,
    *,
    user_text: str,
    timezone: str,
) -> tuple[IntentResult | None, object | None]:
    """`(result, response)`. `response` carries `.usage` for the ledger --
    app/core/turn.py records it under parse.PLANNER_CATEGORY the same way
    it already records welfare's own usage, whatever `result` came out to
    (a call that ran was billed, matching welfare.classify's own rule).

    Callers must already have decided this turn is eligible to run at
    all (persona mode, not a hard pause) -- see the module docstring.
    `settings.PLANNER_INTENT` and the prefilter are checked here too, so
    a caller that forgets either still gets the safe (no call) behaviour.
    """
    if not settings.PLANNER_INTENT or not prefilter_hit(user_text):
        return None, None

    today = clock_module.local_date(clock, timezone)
    try:
        response = await asyncio.wait_for(
            provider.complete(
                [
                    LLMMessage(
                        role="system",
                        content=_INTENT_PROMPT.format(today=today.isoformat()),
                    ),
                    LLMMessage(role="user", content=user_text),
                ],
                conversation_id="anchor-planner-intent",
                json_schema=INTENT_SCHEMA,
            ),
            timeout=settings.PLANNER_INTENT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("planner intent timed out", extra={"event": "TimeoutError"})
        return None, None
    except Exception as exc:
        # app/log.py convention (see welfare.classify): the exception
        # type name only, never a traceback or the user's text.
        logger.warning("planner intent call failed", extra={"event": type(exc).__name__})
        return None, None

    result = _to_result(response.text, clock=clock, timezone=timezone)
    return result, response
