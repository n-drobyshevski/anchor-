"""The `prebrief` idle kind (Phase 6 plan section 6.4; milestone 6c).

Drafts up to three short notes for **tomorrow's** morning message,
stored in `brief_note` keyed on the local date they are *for*. It never
writes to `memory` or `notebook_entry` -- nothing here is reversible
(plan section 6's own list of idle-writable tables gives `brief_note`
no `idle_change` entry, unlike `consolidate`/`reflect`), and
`idle_run.reversible` is left `False` for this kind (app/core/idle/
runner.py).

**Input** (plan section 6.4): today's check-in and its order results,
the due action, open threads, and tomorrow's due orders. Welfare, OOC
and canned rows never enter it -- there simply are none among these:
`Checkin`/`CheckinOrderResult`/`StandingOrder`/`UserState.due_action`
carry no message kind at all, and `notebook_module.active_entries`
already excludes anything but Anchor's own working notes.

**"Morning intent disabled", precisely.** There is no morning-only
switch; the morning send is part of the routine itself
(app/core/outbound_gate.py's own comment on `MORNING`: "No extra rule").
So the kind rule (app/core/idle/gate.py's `_prebrief_rule`) reduces
"disabled" to `settings.OUTBOUND_ENABLED` being false -- the same
global switch that gate's own row 1 checks before it ever asks which
proactive kind is being planned. `IdleConfig.morning_enabled` carries
that setting in so this module's `find_due_orders`/gate stay agreeing
with `config_from_settings`.

**Why this module does not import `app.core.orders`.** Same reasoning
as `app/core/idle/reflect.py`'s own docstring: `tests/test_idle_isolation.py`
bans the whole module ("standing orders -- not an idle-writable
table"), so `_due_orders` below duplicates `orders._is_due`'s cadence
logic directly against `StandingOrder`, read-only.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import clock as clock_module
from app.core import notebook as notebook_module
from app.core import safety_events
from app.core.clock import Clock
from app.core.extract import parse_json
from app.core.screen import screen
from app.db.models import BriefNote, Checkin, CheckinOrderResult, StandingOrder, UserState
from app.llm.provider import JSONSchema, LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

PREBRIEF_CATEGORY = "idle:prebrief"

NOTE_MAX_LEN = 160
NOTES_MAX_COUNT = 3

# Duplicated from app/core/orders.py's own constants -- see the module
# docstring on why this module may not import that one.
_ORDER_ACTIVE = "active"
_DAILY = "daily"
_WEEKDAYS = "weekdays"
_WEEKLY = "weekly"
_ONCE = "once"

PREBRIEF_SCHEMA = JSONSchema(
    name="anchor_prebrief",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["notes"],
        "properties": {
            "notes": {"type": "array", "items": {"type": "string"}},
        },
    },
)

PREBRIEF_PROMPT = (
    "Ты готовишь черновик заметок для завтрашнего утреннего сообщения Anchor "
    "пользователю. По итогам сегодняшнего дня и того, что известно на завтра, "
    "сформулируй до 3 коротких заметок (каждая не длиннее 160 символов) -- "
    "то, что стоит держать в уме, говоря с пользователем завтра утром. "
    "Пиши по-русски, коротко, фактами, без диагнозов, домыслов и советов "
    "о необратимых изменениях. Если сказать нечего, верни пустой список."
)


@dataclasses.dataclass(frozen=True)
class PrebriefResult:
    notes: tuple[str, ...]
    preempted: bool = False


def _is_due(order: StandingOrder, local_date: datetime.date) -> bool:
    if order.cadence == _DAILY:
        return True
    if order.cadence == _WEEKDAYS:
        return local_date.isoweekday() <= 5
    if order.cadence == _WEEKLY:
        return local_date.isoweekday() == order.weekday
    if order.cadence == _ONCE:
        return True
    return False


async def _due_orders(session: AsyncSession, local_date: datetime.date) -> list[str]:
    result = await session.execute(
        select(StandingOrder).where(StandingOrder.status == _ORDER_ACTIVE).order_by(StandingOrder.id)
    )
    orders = list(result.scalars().all())
    return [row.text for row in orders if _is_due(row, local_date)]


async def _today_checkin(
    session: AsyncSession, local_date: datetime.date
) -> tuple[Checkin, list[tuple[str, str]]] | None:
    result = await session.execute(select(Checkin).where(Checkin.local_date == local_date))
    checkin = result.scalar_one_or_none()
    if checkin is None:
        return None
    results = await session.execute(
        select(StandingOrder.text, CheckinOrderResult.result)
        .join(CheckinOrderResult, CheckinOrderResult.order_id == StandingOrder.id)
        .where(CheckinOrderResult.checkin_id == checkin.id)
        .order_by(StandingOrder.id)
    )
    return checkin, [(text, value) for text, value in results.all()]


def validate(payload: dict) -> list[str]:
    """`{"notes": [...]}` -> a validated list of at most `NOTES_MAX_COUNT`
    strings, each at most `NOTE_MAX_LEN` chars and screened -- a note
    that fails any check is dropped, not the whole payload (same
    "drop the offending item" posture as every other idle validator)."""
    if not isinstance(payload, dict):
        return []
    raw_notes = payload.get("notes")
    if not isinstance(raw_notes, list):
        return []
    notes: list[str] = []
    for raw in raw_notes:
        if len(notes) >= NOTES_MAX_COUNT:
            break
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text or len(text) > NOTE_MAX_LEN:
            continue
        if not screen(text).ok:
            continue
        notes.append(text)
    return notes


def build_prebrief_input(
    *,
    today: datetime.date,
    checkin: tuple[Checkin, list[tuple[str, str]]] | None,
    due_action: str | None,
    thread_lines: list[str],
    tomorrow_orders: list[str],
) -> str:
    lines = [f"## Сегодня: {today.isoformat()}"]

    lines.append("")
    lines.append("## Чек-ин сегодня")
    if checkin is None:
        lines.append("(чек-ина не было)")
    else:
        row, results = checkin
        parts = []
        if row.day_rating:
            parts.append(f"день {row.day_rating}/5")
        if row.due_result:
            parts.append(f"действие: {row.due_result}")
        if results:
            tail = "; ".join(f"«{text}» — {result}" for text, result in results)
            parts.append(f"договорённости: {tail}")
        lines.append(" · ".join(parts) if parts else "(без деталей)")

    lines.append("")
    lines.append(f"Главное действие сегодня: {due_action or 'нет'}")

    lines.append("")
    lines.append("## Незакрытые темы")
    if thread_lines:
        lines.extend(f"- {line}" for line in thread_lines)
    else:
        lines.append("(нет)")

    lines.append("")
    lines.append("## Договорённости на завтра")
    if tomorrow_orders:
        lines.extend(f"- {line}" for line in tomorrow_orders)
    else:
        lines.append("(нет)")

    return "\n".join(lines)


async def note_exists(session: AsyncSession, local_date: datetime.date) -> bool:
    """Whether `brief_note` already has a row for `local_date` -- shared
    by the gate's kind rule (app/core/idle/facts.py) and the job's own
    apply-time re-check above, so the two can never disagree."""
    return await session.get(BriefNote, local_date) is not None


async def run_prebrief(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    safety_provider: LLMProvider,
    clock: Clock,
    *,
    run_id: int,
    started_at: datetime.datetime,
    timezone: str,
) -> PrebriefResult:
    """The `prebrief` idle kind body (plan section 6.4), called by
    app/core/idle/runner.py. Single transaction, same shape as
    app/core/idle/consolidate.py/reflect.py: reads and the model call
    happen with no open write transaction, the store happens in one
    transaction opened after the model call and after the in-job
    preemption re-check."""
    from app.core.idle.runner import RunContext, is_preempted

    today = clock_module.local_date(clock, timezone)
    tomorrow = today + datetime.timedelta(days=1)

    async with session_factory() as session:
        checkin = await _today_checkin(session, today)
        state = (await session.execute(select(UserState).where(UserState.id == 1))).scalar_one_or_none()
        due_action = state.due_action if state is not None else None
        view = await notebook_module.active_entries(session)
        thread_lines = [text for _, text, _ in view.threads]
        tomorrow_orders = await _due_orders(session, tomorrow)

        user_text = build_prebrief_input(
            today=today, checkin=checkin, due_action=due_action,
            thread_lines=thread_lines, tomorrow_orders=tomorrow_orders,
        )

    response = await safety_provider.complete(
        [
            LLMMessage(role="system", content=PREBRIEF_PROMPT),
            LLMMessage(role="user", content=user_text),
        ],
        conversation_id=f"anchor-idle-prebrief-{run_id}",
        json_schema=PREBRIEF_SCHEMA,
    )

    async with session_factory() as session:
        ctx = RunContext(
            session=session, settings=settings, clock=clock, run_id=run_id,
            kind="prebrief", started_at=started_at, timezone=timezone,
        )
        await ctx.charge(response.usage, response.model)

        payload = parse_json(response.text)
        await safety_events.record_in(
            session, clock=clock, timezone=timezone, kind=safety_events.NOTEBOOK,
            outcome=safety_events.PARSE_FAIL if payload is None else "ok", model=response.model,
        )
        await session.commit()

        if payload is None:
            logger.warning("idle prebrief returned unparseable output", extra={"run_id": run_id})
            return PrebriefResult(notes=())

        notes = validate(payload)

    async with session_factory() as session:
        if await is_preempted(session, clock, started_at):
            return PrebriefResult(notes=(), preempted=True)

        # Re-check: another run could have written tomorrow's note
        # between the kind rule's own check and now (the same race
        # every idle apply-time re-check exists for).
        existing = await session.get(BriefNote, tomorrow)
        if existing is not None:
            return PrebriefResult(notes=())

        session.add(BriefNote(local_date=tomorrow, notes=notes))
        await session.commit()

    logger.info("idle prebrief run done", extra={"run_id": run_id, "count": len(notes)})
    return PrebriefResult(notes=tuple(notes))


__all__ = [
    "NOTES_MAX_COUNT",
    "NOTE_MAX_LEN",
    "PREBRIEF_CATEGORY",
    "PREBRIEF_PROMPT",
    "PREBRIEF_SCHEMA",
    "PrebriefResult",
    "build_prebrief_input",
    "note_exists",
    "run_prebrief",
    "validate",
]
