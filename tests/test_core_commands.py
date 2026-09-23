"""app/core/commands.py: the W2 extraction of /due, /focus, /quiet, /tz
and proposal expiry out of app/tg/router.py (roadmap section 3).

These call the module directly, with no Dispatcher/Router involved --
tests/test_state_commands.py, tests/test_quiet_tz.py and
tests/test_proposals.py already pin that Telegram's own behaviour did
not move under this refactor (they exercise the *handlers*, which now
call this module). This file is the other half: does each function do,
on its own, exactly what its docstring in app/core/commands.py says.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.core import commands, proposal
from app.core.clock import SystemClock
from app.core.outbound import cancel_outbound
from app.db.models import Outbound, StateChange, UserState

pytestmark = pytest.mark.asyncio

TIMEZONE = "Europe/Paris"
DAY = datetime.date(2026, 9, 23)


async def _seed(sessionmaker, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone=TIMEZONE, **state))
        await session.commit()


async def _state(sessionmaker) -> UserState:
    async with sessionmaker() as session:
        return await session.get(UserState, 1)


async def _changes(sessionmaker) -> list[StateChange]:
    async with sessionmaker() as session:
        return list((await session.execute(select(StateChange))).scalars())


# --- set_due -------------------------------------------------------------


async def test_set_due_sets_action_and_timestamp_with_the_given_source(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        state = await commands.set_due(session, clock, "сдать отчёт", "web")

    assert state.due_action == "сдать отчёт"
    assert state.due_set_at is not None
    changes = await _changes(sessionmaker)
    assert {(c.field, c.source) for c in changes} == {
        ("due_action", "web"),
        ("due_set_at", "web"),
    }


async def test_set_due_strips_whitespace(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        state = await commands.set_due(session, clock, "  сдать отчёт  ", "command")
    assert state.due_action == "сдать отчёт"


async def test_set_due_with_empty_text_clears_both_fields(sessionmaker, clock):
    await _seed(
        sessionmaker, due_action="старое", due_set_at=datetime.datetime.now(datetime.timezone.utc)
    )
    async with sessionmaker() as session:
        state = await commands.set_due(session, clock, "", "command")
    assert state.due_action is None
    assert state.due_set_at is None


async def test_set_due_with_none_text_clears_both_fields(sessionmaker, clock):
    await _seed(
        sessionmaker, due_action="старое", due_set_at=datetime.datetime.now(datetime.timezone.utc)
    )
    async with sessionmaker() as session:
        state = await commands.set_due(session, clock, None, "command")
    assert state.due_action is None
    assert state.due_set_at is None


# --- set_focus -------------------------------------------------------------


async def test_set_focus_on_sets_since(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        state = await commands.set_focus(session, clock, True, "web")
    assert state.focus_on is True
    assert state.focus_since is not None
    changes = await _changes(sessionmaker)
    assert {(c.field, c.source) for c in changes} == {
        ("focus_on", "web"),
        ("focus_since", "web"),
    }


async def test_set_focus_off_clears_since(sessionmaker, clock):
    await _seed(
        sessionmaker, focus_on=True, focus_since=datetime.datetime.now(datetime.timezone.utc)
    )
    async with sessionmaker() as session:
        state = await commands.set_focus(session, clock, False, "web")
    assert state.focus_on is False
    assert state.focus_since is None


# --- set_quiet -------------------------------------------------------------


async def test_set_quiet_with_until_sets_the_field_and_cancels_outbound(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind="morning", local_date=DAY, bucket=0,
                planned_for=datetime.datetime.now(datetime.timezone.utc), status="planned",
            )
        )
        await session.commit()

    until = clock.now_utc() + datetime.timedelta(hours=2)
    async with sessionmaker() as session:
        state = await commands.set_quiet(session, clock, until, "web")
    assert state.quiet_until == until

    async with sessionmaker() as session:
        rows = list((await session.execute(select(Outbound))).scalars())
    assert [r.status for r in rows] == ["cancelled"], "setting quiet cancels what is planned"


async def test_set_quiet_off_clears_and_does_not_cancel_anything(sessionmaker, clock):
    await _seed(sessionmaker, quiet_until=clock.now_utc() + datetime.timedelta(hours=5))
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind="morning", local_date=DAY, bucket=0,
                planned_for=datetime.datetime.now(datetime.timezone.utc), status="planned",
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        state = await commands.set_quiet(session, clock, None, "web")
    assert state.quiet_until is None

    async with sessionmaker() as session:
        rows = list((await session.execute(select(Outbound))).scalars())
    assert [r.status for r in rows] == ["planned"], "turning quiet off must not cancel anything"


# --- set_timezone ------------------------------------------------------


async def test_set_timezone_sets_the_zone(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        state = await commands.set_timezone(session, "Asia/Tokyo", "web")
    assert state.timezone == "Asia/Tokyo"
    changes = await _changes(sessionmaker)
    assert [(c.field, c.new_value, c.source) for c in changes] == [
        ("timezone", "Asia/Tokyo", "web")
    ]


@pytest.mark.parametrize("bad", ["Europe/Atlantis", "GMT+3", "../../etc/passwd", ""])
async def test_set_timezone_rejects_an_unknown_zone_and_changes_nothing(sessionmaker, clock, bad):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        with pytest.raises(commands.InvalidTimezone):
            await commands.set_timezone(session, bad, "web")

    assert (await _state(sessionmaker)).timezone == TIMEZONE
    assert await _changes(sessionmaker) == []


# --- expire_proposal_for ------------------------------------------------


async def test_expire_proposal_for_expires_a_matching_pending_proposal(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        created, _ = await proposal.create(
            session, clock, field=proposal.DUE_ACTION, value="что-то", reason=None
        )
        proposal_id = created.id

    async with sessionmaker() as session:
        expired = await commands.expire_proposal_for(session, clock, proposal.DUE_ACTION)

    assert expired is not None
    assert expired.id == proposal_id
    assert expired.status == proposal.EXPIRED
    assert expired.decided_at is not None


async def test_expire_proposal_for_leaves_a_different_field_alone(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        created, _ = await proposal.create(
            session, clock, field=proposal.FOCUS_ON, value="on", reason=None
        )
        proposal_id = created.id

    async with sessionmaker() as session:
        expired = await commands.expire_proposal_for(session, clock, proposal.DUE_ACTION)

    assert expired is None
    async with sessionmaker() as session:
        row = await session.get(proposal.Proposal, proposal_id)
    assert row.status == proposal.PENDING


async def test_expire_proposal_for_with_no_pending_proposal_returns_none(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        expired = await commands.expire_proposal_for(session, clock, proposal.DUE_ACTION)
    assert expired is None
