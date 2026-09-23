"""The idle planner: priority, one at a time, the dedup key, skip-
transition logging (approved plan §5's test list for milestone 6a)."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle import BACKFILL
from app.core.idle.gate import NOTHING_TO_BACKFILL, USER_ACTIVE
from app.core.idle.planner import PRIORITY, plan_idle
from app.db.models import IdleRun, Job, Message, Scene, UserState

pytestmark = pytest.mark.asyncio

TZ = "Europe/Paris"


def _clock() -> FrozenClock:
    return FrozenClock(datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc))


async def _seed_state(sessionmaker, **overrides) -> None:
    values = dict(id=1, chat_id=555, timezone=TZ)
    values.update(overrides)
    async with sessionmaker() as session:
        session.add(UserState(**values))
        await session.commit()


async def _seed_backfill_candidate(sessionmaker, clock) -> int:
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene = Scene(
            started_at=now - datetime.timedelta(hours=2),
            ended_at=now - datetime.timedelta(hours=1),
        )
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        session.add_all(
            [
                Message(role="user", content="1", ooc=False, kind="chat", scene_id=scene.id),
                Message(role="assistant", content="2", ooc=False, kind="chat", scene_id=scene.id),
                Message(role="user", content="3", ooc=False, kind="chat", scene_id=scene.id),
            ]
        )
        await session.commit()
        return scene.id


async def test_priority_picks_backfill_first(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_backfill_candidate(sessionmaker, clock)

    async with sessionmaker() as session:
        run_id = await plan_idle(session, Settings(), clock)

    assert run_id is not None
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        assert run.kind == BACKFILL
        assert run.status == "queued"
        jobs = (await session.execute(select(Job))).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].kind == "idle_run"
        assert jobs[0].payload == {"run_id": run_id}


async def test_priority_order_matches_the_plan():
    assert PRIORITY == (
        "backfill", "consolidate", "prebrief", "reflect", "critique", "research", "canary",
    )


async def test_one_idle_job_at_a_time(sessionmaker):
    """A second call finds the run already busy and plans nothing."""
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_backfill_candidate(sessionmaker, clock)

    async with sessionmaker() as session:
        first_id = await plan_idle(session, Settings(), clock)
    assert first_id is not None

    async with sessionmaker() as session:
        second_id = await plan_idle(session, Settings(), clock)
    assert second_id is None

    async with sessionmaker() as session:
        runs = (await session.execute(select(IdleRun))).scalars().all()
    # Only the one queued run -- no skip row either, since "busy" would
    # be a new skip reason worth recording, but see the dedup test below
    # for that behaviour specifically.
    assert len([r for r in runs if r.status == "queued"]) == 1


async def test_dedup_key_prevents_a_duplicate_job(sessionmaker):
    """Calling plan_idle twice with the run already gone from `active`
    (status flipped to done) still respects the per-day sequence number,
    never reusing dedup keys across separate runs."""
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_backfill_candidate(sessionmaker, clock)

    async with sessionmaker() as session:
        first_id = await plan_idle(session, Settings(), clock)
    async with sessionmaker() as session:
        run = await session.get(IdleRun, first_id)
        run.status = "done"
        await session.commit()

    await _seed_backfill_candidate(sessionmaker, clock)
    async with sessionmaker() as session:
        second_id = await plan_idle(session, Settings(), clock)

    assert second_id is not None
    assert second_id != first_id
    async with sessionmaker() as session:
        jobs = (await session.execute(select(Job).order_by(Job.id))).scalars().all()
    assert len(jobs) == 2
    assert jobs[0].dedup_key != jobs[1].dedup_key


async def test_no_candidates_records_a_skip_row_with_the_kind_rule_reason(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    # No backfill candidates seeded at all.

    async with sessionmaker() as session:
        result = await plan_idle(session, Settings(), clock)
    assert result is None

    async with sessionmaker() as session:
        runs = (await session.execute(select(IdleRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].status == "skipped"
    assert runs[0].skip_reason == NOTHING_TO_BACKFILL


async def test_skip_is_recorded_only_when_the_reason_changes(sessionmaker):
    """The transition-only logging: the same reason twice writes one row,
    not two."""
    clock = _clock()
    await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        await plan_idle(session, Settings(), clock)
    async with sessionmaker() as session:
        await plan_idle(session, Settings(), clock)

    async with sessionmaker() as session:
        runs = (await session.execute(select(IdleRun))).scalars().all()
    assert len(runs) == 1


async def test_skip_reason_change_writes_a_second_row(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        await plan_idle(session, Settings(), clock)  # nothing_to_backfill

    # Now the user is active -- a different, higher-priority reason.
    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        state.last_user_msg_at = clock.now_utc()
        await session.commit()

    async with sessionmaker() as session:
        await plan_idle(session, Settings(), clock)

    async with sessionmaker() as session:
        runs = (
            await session.execute(select(IdleRun).order_by(IdleRun.id))
        ).scalars().all()
    assert [r.skip_reason for r in runs] == [NOTHING_TO_BACKFILL, USER_ACTIVE]


async def test_idle_disabled_plans_nothing_and_records_no_skip(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        result = await plan_idle(session, Settings(IDLE_ENABLED=False), clock)
    assert result is None

    async with sessionmaker() as session:
        runs = (await session.execute(select(IdleRun))).scalars().all()
    assert runs == []


async def test_a_preempted_run_does_not_reuse_its_dedup_key(sessionmaker):
    """A skipped run keeps its job row; counting only non-skipped runs
    would hand the next run the same key, the insert would be dropped,
    and a queued run with no job would hold `busy` forever."""
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_backfill_candidate(sessionmaker, clock)

    async with sessionmaker() as session:
        first = await plan_idle(session, Settings(), clock)
    async with sessionmaker() as session:
        run = await session.get(IdleRun, first)
        run.status = "skipped"
        run.skip_reason = "preempted"
        await session.commit()

    async with sessionmaker() as session:
        second = await plan_idle(session, Settings(), clock)

    assert second is not None and second != first
    async with sessionmaker() as session:
        keys = sorted((await session.execute(select(Job.dedup_key))).scalars().all())
    local = "2026-09-23"
    assert keys == [f"idle:backfill:{local}:1", f"idle:backfill:{local}:2"]


async def test_a_dedup_collision_leaves_no_orphan_run(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_backfill_candidate(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add(Job(kind="idle_run", payload={}, dedup_key="idle:backfill:2026-09-23:1"))
        await session.commit()

    async with sessionmaker() as session:
        assert await plan_idle(session, Settings(), clock) is None

    async with sessionmaker() as session:
        runs = (
            await session.execute(select(IdleRun).where(IdleRun.status == "queued"))
        ).scalars().all()
    assert runs == []


async def test_a_stale_active_run_is_failed_so_idle_is_not_busy_forever(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_backfill_candidate(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add(
            IdleRun(
                kind="backfill",
                local_date=datetime.date(2026, 9, 23),
                status="running",
                created_at=datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=1),
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        run_id = await plan_idle(session, Settings(), clock)

    assert run_id is not None
    async with sessionmaker() as session:
        stale = (
            await session.execute(select(IdleRun).where(IdleRun.skip_reason == "stale"))
        ).scalars().all()
    assert len(stale) == 1 and stale[0].status == "failed"


async def test_a_run_between_two_equal_skips_starts_a_new_episode(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker, last_user_msg_at=clock.now_utc())
    async with sessionmaker() as session:
        await plan_idle(session, Settings(), clock)
        session.add(
            IdleRun(kind="backfill", local_date=datetime.date(2026, 9, 23), status="done")
        )
        await session.commit()
    async with sessionmaker() as session:
        await plan_idle(session, Settings(), clock)

    async with sessionmaker() as session:
        skips = (
            await session.execute(select(IdleRun).where(IdleRun.status == "skipped"))
        ).scalars().all()
    assert [s.skip_reason for s in skips] == [USER_ACTIVE, USER_ACTIVE]


async def test_backfill_stops_at_its_daily_limit(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_backfill_candidate(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add_all(
            [
                IdleRun(kind="backfill", local_date=datetime.date(2026, 9, 23), status="done")
                for _ in range(3)
            ]
        )
        await session.commit()

    async with sessionmaker() as session:
        assert await plan_idle(session, Settings(), clock) is None


async def test_a_running_idle_job_is_not_recorded_as_a_skip(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_backfill_candidate(sessionmaker, clock)
    async with sessionmaker() as session:
        assert await plan_idle(session, Settings(), clock) is not None
    async with sessionmaker() as session:
        assert await plan_idle(session, Settings(), clock) is None

    async with sessionmaker() as session:
        skips = (
            await session.execute(select(IdleRun).where(IdleRun.status == "skipped"))
        ).scalars().all()
    assert skips == []
