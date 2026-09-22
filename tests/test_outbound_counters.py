"""Outbound counters, the gate's loaders, and the schema (phase-3 plan sections 4, 13).

Three things are under test here, all of them the database half of
Phase 3 that app/core/outbound_gate.py deliberately cannot see:

1. **The counters.** An inbound update resets `ignored_in_row`; a sent
   outbound increments it; three ignored gates everything.
2. **set_counters' allow-list.** This is the one function in the repo
   that writes `user_state` without an audit row, so the Phase 2
   invariant -- no automated path writes intensity, persona_active,
   focus_on, due_action or streak -- has to be re-established for it,
   structurally and not by inspection.
3. **Exactly-once.** `unique (kind, local_date, bucket)` is what makes
   a duplicate heartbeat or an overlapping redeploy produce one message
   instead of two, so the constraint itself is tested, not assumed.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.core.clock import FrozenClock, combine_local, local_date
from app.core.outbound import (
    SENT,
    checkin_done_today,
    gate_state_from,
    last_sent_at,
    last_skip_reason,
    load_gate_inputs,
    next_planned,
    record_inbound,
    record_outbound_sent,
    record_welfare,
    sent_today,
)
from app.core.outbound_gate import (
    IGNORED,
    MORNING,
    OK,
    SILENCE,
    TICK,
    GateConfig,
    GateCounts,
    GateFacts,
    gate,
)
from app.core.state import COUNTER_FIELDS, get_state, set_counters
from app.config import Settings
from app.db.models import Checkin, Message, Outbound, SpendLedger, StateChange, UserState

CHAT_ID = 4242
PARIS = "Europe/Paris"


def at(year, month, day, hour=0, minute=0) -> FrozenClock:
    return FrozenClock(
        combine_local(datetime.date(year, month, day), datetime.time(hour, minute), PARIS)
    )


async def _seed_state(sessionmaker, **fields) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, **fields))
        await session.commit()


async def _add_outbound(sessionmaker, **fields) -> int:
    defaults = dict(
        kind=MORNING,
        local_date=datetime.date(2026, 9, 22),
        bucket=0,
        planned_for=datetime.datetime(2026, 9, 22, 7, 0, tzinfo=datetime.timezone.utc),
        status="planned",
    )
    defaults.update(fields)
    async with sessionmaker() as session:
        row = Outbound(**defaults)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


# --- the counters ------------------------------------------------------


async def test_an_inbound_update_stamps_the_time_and_clears_the_backoff(sessionmaker):
    await _seed_state(sessionmaker, ignored_in_row=2)
    clock = at(2026, 9, 22, 12, 0)

    async with sessionmaker() as session:
        state = await record_inbound(session, clock)

    assert state.ignored_in_row == 0
    assert state.last_user_msg_at == clock.now_utc()


async def test_a_sent_outbound_stamps_the_time_and_counts_as_ignored(sessionmaker):
    await _seed_state(sessionmaker)
    clock = at(2026, 9, 22, 9, 0)

    async with sessionmaker() as session:
        state = await record_outbound_sent(session, clock)

    assert state.ignored_in_row == 1
    assert state.last_outbound_at == clock.now_utc()


async def test_the_increment_accumulates_across_sends(sessionmaker):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        for hour in (9, 13, 17):
            await record_outbound_sent(session, at(2026, 9, 22, hour, 0))
        state = await get_state(session)
    assert state.ignored_in_row == 3


async def test_three_ignored_then_everything_is_gated_then_a_message_recovers_it(
    sessionmaker,
):
    """The full loop of plan section 11's back-off, end to end."""
    await _seed_state(sessionmaker)
    clock = at(2026, 9, 22, 12, 0)

    async with sessionmaker() as session:
        for _ in range(3):
            await record_outbound_sent(session, clock)
        state = await get_state(session)

    blocked = gate(
        MORNING,
        gate_state_from(state),
        clock.now_utc(),
        GateCounts(),
        GateFacts(),
        GateConfig(),
    )
    assert blocked == (False, IGNORED)

    # The user writes. Anchor is allowed to speak again.
    async with sessionmaker() as session:
        state = await record_inbound(session, clock)

    allowed = gate(
        MORNING,
        gate_state_from(state),
        clock.now_utc(),
        GateCounts(),
        GateFacts(),
        GateConfig(),
    )
    assert allowed == (True, OK)


async def test_a_welfare_trigger_stamps_the_cooldown(sessionmaker):
    await _seed_state(sessionmaker)
    clock = at(2026, 9, 22, 15, 0)
    async with sessionmaker() as session:
        state = await record_welfare(session, clock)
    assert state.welfare_at == clock.now_utc()


async def test_the_counters_write_no_audit_rows(sessionmaker):
    """state_change is a log of decisions. Two extra inserts on every
    inbound message would bury the rows a human wants to read."""
    await _seed_state(sessionmaker)
    clock = at(2026, 9, 22, 12, 0)

    async with sessionmaker() as session:
        await record_inbound(session, clock)
        await record_outbound_sent(session, clock)
        await record_welfare(session, clock)
        count = await session.scalar(select(func.count()).select_from(StateChange))

    assert count == 0


# --- the allow-list ----------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["persona_active", "intensity", "focus_on", "due_action", "streak", "timezone"],
)
def test_the_sensitive_fields_are_not_in_the_counter_allow_list(field):
    """Phase 2's invariant (plan section 13) has to survive a function
    that writes user_state on every single inbound update."""
    assert field not in COUNTER_FIELDS


@pytest.mark.parametrize(
    "field, value",
    [
        ("persona_active", False),
        ("intensity", 5),
        ("focus_on", True),
        ("due_action", "сдать отчёт"),
        ("streak", 99),
    ],
)
async def test_set_counters_refuses_a_sensitive_field(sessionmaker, field, value):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        with pytest.raises(ValueError):
            await set_counters(session, **{field: value})

        state = await get_state(session)

    # And nothing was written on the way to raising.
    assert state.persona_active is True
    assert state.intensity == 3
    assert state.focus_on is False
    assert state.due_action is None
    assert state.streak == 0


async def test_set_counters_with_nothing_to_write_is_a_no_op(sessionmaker):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        state = await set_counters(session)
    assert state.ignored_in_row == 0


async def test_the_allow_list_is_exactly_the_plans_four_counters(sessionmaker):
    assert COUNTER_FIELDS == {
        "last_user_msg_at",
        "last_outbound_at",
        "ignored_in_row",
        "welfare_at",
    }


# --- exactly-once ------------------------------------------------------


async def test_one_row_per_kind_local_date_and_bucket(sessionmaker):
    await _add_outbound(sessionmaker)
    with pytest.raises(IntegrityError):
        await _add_outbound(sessionmaker)


async def test_a_duplicate_plan_collapses_to_nothing_with_on_conflict(sessionmaker):
    """How the heartbeat will actually insert: two overlapping processes
    during a redeploy must produce one message, not two."""
    values = dict(
        kind=MORNING,
        local_date=datetime.date(2026, 9, 22),
        bucket=0,
        planned_for=datetime.datetime(2026, 9, 22, 7, 0, tzinfo=datetime.timezone.utc),
    )
    inserted = []
    for _ in range(2):
        async with sessionmaker() as session:
            result = await session.execute(
                pg_insert(Outbound)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["kind", "local_date", "bucket"]
                )
                .returning(Outbound.id)
            )
            await session.commit()
            inserted.append(result.first())

    assert inserted[0] is not None
    assert inserted[1] is None


async def test_a_different_bucket_is_a_different_row(sessionmaker):
    """Two ticks in one day are distinguished by their local hour."""
    await _add_outbound(sessionmaker, kind=TICK, bucket=10)
    await _add_outbound(sessionmaker, kind=TICK, bucket=14)
    async with sessionmaker() as session:
        count = await session.scalar(select(func.count()).select_from(Outbound))
    assert count == 2


@pytest.mark.parametrize("bad", ["morning_nag", "chat", ""])
async def test_the_kind_check_constraint_rejects_anything_unplanned(sessionmaker, bad):
    with pytest.raises(IntegrityError):
        await _add_outbound(sessionmaker, kind=bad)


@pytest.mark.parametrize("bad", ["pending", "done", ""])
async def test_the_status_check_constraint_rejects_anything_unplanned(sessionmaker, bad):
    with pytest.raises(IntegrityError):
        await _add_outbound(sessionmaker, status=bad)


async def test_a_tick_note_longer_than_the_plans_limit_is_rejected(sessionmaker):
    with pytest.raises(IntegrityError):
        await _add_outbound(sessionmaker, kind=TICK, bucket=10, tick_note="я" * 121)


async def test_a_tick_note_at_the_limit_is_accepted(sessionmaker):
    await _add_outbound(sessionmaker, kind=TICK, bucket=10, tick_note="я" * 120)


async def test_a_message_can_carry_an_outbound_id_only_once(sessionmaker):
    """message.outbound_id is the send job's idempotency key, exactly as
    reply_to_update is for a reply."""
    outbound_id = await _add_outbound(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            Message(role="assistant", content="x", kind="outbound", outbound_id=outbound_id)
        )
        await session.commit()

    with pytest.raises(IntegrityError):
        async with sessionmaker() as session:
            session.add(
                Message(
                    role="assistant", content="y", kind="outbound", outbound_id=outbound_id
                )
            )
            await session.commit()


async def test_outbound_is_an_accepted_message_kind(sessionmaker):
    """Plan section 7 puts proactive messages in the persona transcript,
    so the Phase 2 check constraint had to grow a value."""
    async with sessionmaker() as session:
        session.add(Message(role="assistant", content="доброе утро", kind="outbound"))
        await session.commit()
        count = await session.scalar(
            select(func.count()).select_from(Message).where(Message.kind == "outbound")
        )
    assert count == 1


# --- the loaders -------------------------------------------------------


async def test_sent_today_counts_only_delivered_rows_on_that_local_date(sessionmaker):
    day = datetime.date(2026, 9, 22)
    await _add_outbound(sessionmaker, kind=MORNING, local_date=day, status=SENT)
    await _add_outbound(sessionmaker, kind="evening_nag", local_date=day, status="skipped")
    await _add_outbound(sessionmaker, kind=TICK, bucket=10, local_date=day, status=SENT)
    await _add_outbound(
        sessionmaker, kind=MORNING, local_date=datetime.date(2026, 9, 21), status=SENT
    )

    async with sessionmaker() as session:
        assert await sent_today(session, day) == 2
        assert await sent_today(session, day, kind=TICK) == 1
        assert await sent_today(session, datetime.date(2026, 9, 21)) == 1


async def test_last_sent_at_reaches_back_past_today(sessionmaker):
    """The 48h silence rule spans local dates; scoping it to today would
    let a nudge at 23:00 be followed by another at 00:30."""
    earlier = datetime.datetime(2026, 9, 20, 9, 0, tzinfo=datetime.timezone.utc)
    later = datetime.datetime(2026, 9, 22, 9, 0, tzinfo=datetime.timezone.utc)
    await _add_outbound(
        sessionmaker,
        kind=SILENCE,
        local_date=datetime.date(2026, 9, 20),
        status=SENT,
        sent_at=earlier,
    )
    await _add_outbound(
        sessionmaker,
        kind=SILENCE,
        local_date=datetime.date(2026, 9, 22),
        status=SENT,
        sent_at=later,
    )
    async with sessionmaker() as session:
        assert await last_sent_at(session, SILENCE) == later
        assert await last_sent_at(session, MORNING) is None


async def test_next_planned_is_the_soonest_live_row(sessionmaker):
    soon = datetime.datetime(2026, 9, 22, 7, 0, tzinfo=datetime.timezone.utc)
    later = datetime.datetime(2026, 9, 22, 20, 0, tzinfo=datetime.timezone.utc)
    await _add_outbound(sessionmaker, kind="evening_nag", planned_for=later)
    await _add_outbound(sessionmaker, kind=MORNING, planned_for=soon)
    # A cancelled row is not "next", however soon it was.
    await _add_outbound(
        sessionmaker,
        kind=SILENCE,
        planned_for=soon - datetime.timedelta(hours=1),
        status="cancelled",
    )

    async with sessionmaker() as session:
        row = await next_planned(session)
    assert row is not None and row.kind == MORNING


async def test_last_skip_reason_is_todays_most_recent_refusal(sessionmaker):
    day = datetime.date(2026, 9, 22)
    await _add_outbound(
        sessionmaker, kind=MORNING, local_date=day, status="skipped", skip_reason="quiet_cmd"
    )
    await _add_outbound(
        sessionmaker,
        kind="evening_nag",
        local_date=day,
        status="skipped",
        skip_reason="kind_rule:checkin_done",
    )
    async with sessionmaker() as session:
        assert await last_skip_reason(session, day) == "kind_rule:checkin_done"
        assert await last_skip_reason(session, datetime.date(2026, 9, 21)) is None


async def test_checkin_today_reads_completion_not_the_row(sessionmaker):
    """A /checkin that was started and abandoned leaves a row with every
    answer null. Treating that as "checked in" would suppress exactly
    the evening nag that was needed."""
    clock = at(2026, 9, 22, 22, 0)
    await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        session.add(Checkin(local_date=datetime.date(2026, 9, 22)))
        await session.commit()
        state = await get_state(session)
        assert await checkin_done_today(session, state, clock) is False

    # finish() stamps last_checkin_at; that is what counts.
    async with sessionmaker() as session:
        await session.execute(
            UserState.__table__.update()
            .where(UserState.id == 1)
            .values(last_checkin_at=clock.now_utc())
        )
        await session.commit()
        state = await get_state(session)
        assert await checkin_done_today(session, state, clock) is True


async def test_checkin_today_is_measured_in_the_users_zone(sessionmaker):
    """A check-in at 23:30 Paris is today's, even though it is already
    tomorrow in UTC terms for some zones and not others."""
    clock = at(2026, 9, 22, 23, 30)
    await _seed_state(sessionmaker, last_checkin_at=clock.now_utc())
    async with sessionmaker() as session:
        state = await get_state(session)
        assert await checkin_done_today(session, state, clock) is True

    tomorrow = at(2026, 9, 23, 0, 30)
    async with sessionmaker() as session:
        state = await get_state(session)
        assert await checkin_done_today(session, state, tomorrow) is False


async def test_load_gate_inputs_matches_what_the_gate_expects(sessionmaker):
    clock = at(2026, 9, 22, 12, 0)
    await _seed_state(sessionmaker, ignored_in_row=1, focus_on=True)
    day = local_date(clock, PARIS)
    await _add_outbound(sessionmaker, kind=MORNING, local_date=day, status=SENT)
    await _add_outbound(sessionmaker, kind=TICK, bucket=10, local_date=day, status=SENT)
    async with sessionmaker() as session:
        session.add(
            SpendLedger(
                local_date=day, category="chat", usd_cost=decimal.Decimal("0.25")
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        state = await get_state(session)
        gate_state, counts, facts = await load_gate_inputs(
            session, clock, Settings(_env_file=None), state
        )

    assert gate_state.focus_on is True
    assert gate_state.ignored_in_row == 1
    assert counts.sent_today == 2
    assert counts.tick_sent_today == 1
    assert counts.spend_today_usd == decimal.Decimal("0.25")
    assert facts.checkin_today is False
