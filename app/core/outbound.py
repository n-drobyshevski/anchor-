"""Outbound bookkeeping: the counters, the gate's inputs, and the cancel hook.

3a is the half of Phase 3 that touches the database. The other half --
the decision itself -- lives in app/core/outbound_gate.py and is a pure
function, which is the point: everything here is *loading*, so the gate
has no session to reach for even by accident.

Three responsibilities:

1. **Counters** (plan section 4). `record_inbound()` on every inbound
   update, `record_outbound_sent()` on every delivered proactive
   message, `record_welfare()` when the welfare check fires. They go
   through state.set_counters(), which writes no audit row and
   structurally cannot reach persona_active, intensity, focus_on,
   due_action or streak.

2. **Gate inputs.** `load_gate_inputs()` assembles the snapshot the
   gate reads, in one place, so the planning-time call and the
   authoritative send-time call cannot drift apart. They must agree on
   what "sent today" means or the second check stops being a check.

3. **`cancel_outbound()`**, real as of 3b: every planned row becomes
   `cancelled`, and the pending job no-ops when it sees that. Pause,
   welfare and /delete call it; /quiet joins them in 3c.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.outbound_gate import (
    SILENCE,
    TICK,
    GateCounts,
    GateFacts,
    GateState,
)
from app.core.spend import today_usd
from app.core.state import set_counters
from app.db.models import Outbound, UserState

logger = logging.getLogger(__name__)

# Terminal and live statuses, named so queries read as English.
PLANNED = "planned"
SENT = "sent"
SKIPPED = "skipped"
CANCELLED = "cancelled"
FAILED = "failed"


# --- counters (plan section 4) -----------------------------------------


async def record_inbound(session: AsyncSession, clock: Clock) -> UserState:
    """The user did something. Stamp it and clear the back-off.

    Called for *any* inbound update -- plain text, a slash command, or
    a button press. A button is the user being present just as much as
    a sentence is, and a bot that kept counting someone as "ignoring
    me" while they tapped through a check-in would be obviously wrong.

    Resetting `ignored_in_row` here is the whole recovery path from the
    `ignored` gate: after MAX_IGNORED_IN_ROW unanswered messages Anchor
    goes silent until the user writes, and this is the writing.
    """
    return await set_counters(
        session, last_user_msg_at=clock.now_utc(), ignored_in_row=0
    )


async def record_outbound_sent(session: AsyncSession, clock: Clock) -> UserState:
    """A proactive message went out. Stamp it and assume it is unanswered.

    The increment is optimistic in the pessimistic direction: every
    sent message counts as ignored the moment it is sent, and only an
    inbound update takes it back. That ordering is what makes the
    back-off safe across a crash -- a message that was delivered but
    whose reply never arrives leaves the counter raised, which is the
    conservative state to be in.

    A single atomic UPDATE rather than read-modify-write, so this is
    still correct if worker concurrency is ever raised above 1.
    """
    return await set_counters(
        session,
        last_outbound_at=clock.now_utc(),
        ignored_in_row=UserState.ignored_in_row + 1,
    )


async def record_welfare(session: AsyncSession, clock: Clock) -> UserState:
    """The welfare check fired. Starts the WELFARE_COOLDOWN_H window.

    Only the discretionary kinds (silence, tick) are held back by it --
    see the gate. Morning and evening are part of the routine the user
    agreed to and resume when the persona does.
    """
    return await set_counters(session, welfare_at=clock.now_utc())


# --- gate inputs -------------------------------------------------------


async def sent_today(
    session: AsyncSession, local_date: datetime.date, *, kind: str | None = None
) -> int:
    """How many outbound messages were *delivered* on a local date."""
    stmt = (
        select(func.count())
        .select_from(Outbound)
        .where(Outbound.local_date == local_date)
        .where(Outbound.status == SENT)
    )
    if kind is not None:
        stmt = stmt.where(Outbound.kind == kind)
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def last_sent_at(session: AsyncSession, kind: str) -> datetime.datetime | None:
    """When a given kind last went out, across all dates.

    Deliberately not scoped to today: the silence nudge's 48-hour rule
    reaches back further than one local date, and scoping it to today
    would let a nudge sent at 23:00 be followed by another at 00:30.
    """
    result = await session.execute(
        select(func.max(Outbound.sent_at))
        .where(Outbound.kind == kind)
        .where(Outbound.status == SENT)
    )
    return result.scalar_one()


async def next_planned(session: AsyncSession) -> Outbound | None:
    """The soonest still-planned row, for /state (plan section 10)."""
    result = await session.execute(
        select(Outbound)
        .where(Outbound.status == PLANNED)
        .order_by(Outbound.planned_for)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def last_skip_reason(
    session: AsyncSession, local_date: datetime.date
) -> str | None:
    """Today's most recent refusal, for /state."""
    result = await session.execute(
        select(Outbound.skip_reason)
        .where(Outbound.local_date == local_date)
        .where(Outbound.status == SKIPPED)
        .where(Outbound.skip_reason.is_not(None))
        .order_by(Outbound.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def checkin_done_today(
    session: AsyncSession, state: UserState, clock: Clock
) -> bool:
    """Has the user *completed* a check-in today, in their zone?

    Read from `last_checkin_at`, which app/core/checkin.py's finish()
    stamps, rather than from the existence of a `checkin` row. The row
    is created the moment /checkin is typed, with every answer null --
    so "a row exists" is true for an abandoned check-in, and skipping
    the evening nag because the user *started* one and wandered off
    would suppress exactly the reminder that was needed.
    """
    if state.last_checkin_at is None:
        return False
    return (
        clock_module.local_date_of(state.last_checkin_at, state.timezone)
        == clock_module.local_date(clock, state.timezone)
    )


def gate_state_from(state: UserState) -> GateState:
    """Snapshot the user_state fields the gate reads."""
    return GateState(
        timezone=state.timezone,
        persona_active=state.persona_active,
        focus_on=state.focus_on,
        ignored_in_row=state.ignored_in_row,
        quiet_until=state.quiet_until,
        last_user_msg_at=state.last_user_msg_at,
        last_outbound_at=state.last_outbound_at,
        welfare_at=state.welfare_at,
    )


async def load_gate_inputs(
    session: AsyncSession,
    clock: Clock,
    settings: Settings,
    state: UserState,
    *,
    kind: str | None = None,
) -> tuple[GateState, GateCounts, GateFacts]:
    """Assemble everything the gate needs, in one place.

    One loader for both gate runs -- planning and send time -- because
    the send-time check is only a check if it measures the same things
    the planning check did. `kind` narrows the optional queries: the
    silence timestamp and the tick count each cost a round trip that
    only one kind reads, and the heartbeat runs this every 60 seconds.
    """
    today = clock_module.local_date(clock, state.timezone)

    counts = GateCounts(
        sent_today=await sent_today(session, today),
        tick_sent_today=(
            await sent_today(session, today, kind=TICK) if kind in (None, TICK) else 0
        ),
        spend_today_usd=await today_usd(session, clock, state.timezone),
        last_silence_sent_at=(
            await last_sent_at(session, SILENCE) if kind in (None, SILENCE) else None
        ),
    )
    facts = GateFacts(checkin_today=await checkin_done_today(session, state, clock))
    return gate_state_from(state), counts, facts


# --- the cancel hook ---------------------------------------------------


async def cancel_outbound(session: AsyncSession, clock: Clock) -> int:
    """Cancel every planned outbound message. Returns how many.

    Real as of 3b (plan section 6). Called by the HARD pause path, the
    welfare trigger and /delete; /quiet joins them in 3c.

    **The pending jobs are deliberately left alone.** Deleting them
    would be a second thing to get wrong, and a job whose row is no
    longer `planned` already exits at step 1 of
    app/core/outbound_send.py -- before the gate, before the model,
    before anything is spent. One mechanism, checked in the one place
    that matters.

    Status, not deletion: a cancelled row is the record that a message
    *was* going to be sent and was revoked, which is what /state shows
    and what stops the heartbeat re-planning the same intent sixty
    seconds later (see scheduler._already_exists). Deleting the row
    would make the bot forget it had been told to be quiet.

    `clock` is taken and not yet used for a timestamp -- there is no
    `cancelled_at` column in plan section 4. It is in the signature
    because every other write path in this module takes one, and a
    cancel that silently read the wall clock later would be exactly the
    regression tests/test_core_clock_discipline.py exists to prevent.
    """
    result = await session.execute(
        sql_update(Outbound)
        .where(Outbound.status == PLANNED)
        .values(status=CANCELLED)
        .returning(Outbound.id)
    )
    cancelled = [row[0] for row in result.all()]
    await session.commit()
    if cancelled:
        logger.info("outbound cancelled", extra={"count": len(cancelled)})
    return len(cancelled)
