"""app/planner/actions.py's planner_action state machine (P3).

Mirrors tests/test_proposals.py's shape for proposal.py's accept/reject,
plus the two things the design review's "done when" line for P3 calls
out explicitly: a replayed accept enqueues exactly one job, and an
outstanding extractor proposal is untouched by a planner action.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.core import proposal
from app.db.models import Job, PlannerAction
from app.planner import actions
from app.planner.jobs import PLANNER_WRITE

pytestmark = pytest.mark.asyncio

TZ = "Europe/Paris"


async def test_the_dedup_prefix_matches_the_jobs_kind_string() -> None:
    """actions.py cannot import jobs.py (circular), so it enqueues the
    kind as a literal string -- this pins that the literal matches."""
    assert actions.PLANNER_WRITE_DEDUP_PREFIX == "planner_write:"
    assert PLANNER_WRITE == "planner_write"


async def test_create_does_not_expire_a_pending_extractor_proposal(sessionmaker, frozen_clock):
    """Design review, table 1: a planner_action is its own table
    precisely so it cannot trip proposal.create()'s one-pending rule."""
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        pending, _ = await proposal.create(
            session, clock, field=proposal.DUE_ACTION, value="сдать отчёт", reason=None
        )
        await actions.create(session, clock, kind=actions.CREATE_TASK, payload={"title": "Купить молоко"})

    async with sessionmaker() as session:
        still_pending = await proposal.get_pending(session)
    assert still_pending is not None
    assert still_pending.id == pending.id
    assert still_pending.status == proposal.PENDING


async def test_multiple_actions_can_be_pending_at_once(sessionmaker, frozen_clock):
    """Unlike proposal.create(), a second create() does not expire the first."""
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        first = await actions.create(session, clock, kind=actions.CREATE_TASK, payload={"title": "A"})
        second = await actions.create(session, clock, kind=actions.CREATE_TASK, payload={"title": "B"})

    async with sessionmaker() as session:
        row_a = await session.get(PlannerAction, first.id)
        row_b = await session.get(PlannerAction, second.id)
    assert row_a.status == actions.PENDING
    assert row_b.status == actions.PENDING


async def test_accept_enqueues_exactly_one_planner_write_job(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        action = await actions.create(session, clock, kind=actions.CREATE_TASK, payload={"title": "A"})
        decided = await actions.accept(session, clock, action.id)
    assert decided is not None
    assert decided.status == actions.ACCEPTED

    async with sessionmaker() as session:
        rows = (await session.execute(select(Job).where(Job.kind == PLANNER_WRITE))).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload == {"planner_action_id": action.id}
    assert rows[0].dedup_key == f"planner_write:{action.id}"


async def test_a_replayed_accept_enqueues_no_second_job(sessionmaker, frozen_clock):
    """The worker re-runs an update after a crash; a replayed callback
    must not double-enqueue the write (design review P3's "done when")."""
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        action = await actions.create(session, clock, kind=actions.CREATE_TASK, payload={"title": "A"})
        first = await actions.accept(session, clock, action.id)
        second = await actions.accept(session, clock, action.id)

    assert first is not None
    assert second is None

    async with sessionmaker() as session:
        rows = (await session.execute(select(Job).where(Job.kind == PLANNER_WRITE))).scalars().all()
    assert len(rows) == 1


async def test_reject_leaves_no_job_and_is_idempotent(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        action = await actions.create(session, clock, kind=actions.CREATE_TASK, payload={"title": "A"})
        first = await actions.reject(session, clock, action.id)
        second = await actions.reject(session, clock, action.id)

    assert first is not None
    assert first.status == actions.REJECTED
    assert second is None

    async with sessionmaker() as session:
        rows = (await session.execute(select(Job).where(Job.kind == PLANNER_WRITE))).scalars().all()
    assert rows == []


async def test_accept_on_an_already_rejected_action_is_a_noop(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        action = await actions.create(session, clock, kind=actions.CREATE_TASK, payload={"title": "A"})
        await actions.reject(session, clock, action.id)
        result = await actions.accept(session, clock, action.id)
    assert result is None


async def test_accept_on_an_unknown_id_returns_none(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        result = await actions.accept(session, clock, 999999)
    assert result is None


async def test_create_rejects_an_unknown_kind(sessionmaker, frozen_clock) -> None:
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    async with sessionmaker() as session:
        with pytest.raises(ValueError):
            await actions.create(session, clock, kind="delete_everything", payload={})


async def test_count_today_counts_only_rows_created_in_the_local_day(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 23, 30, tz=TZ)
    async with sessionmaker() as session:
        await actions.create(session, clock, kind=actions.CREATE_TASK, payload={"title": "A"})
        await actions.create(session, clock, kind=actions.CREATE_TASK, payload={"title": "B"})
        count_before_midnight = await actions.count_today(session, clock, TZ)
    assert count_before_midnight == 2

    clock.advance(datetime.timedelta(minutes=45))  # crosses local midnight in TZ
    async with sessionmaker() as session:
        count_after_midnight = await actions.count_today(session, clock, TZ)
    assert count_after_midnight == 0
