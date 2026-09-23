"""app/core/scheduler.py's maybe_enqueue_planner_sync, and worker dispatch.

Mirrors tests/test_research_sweeps.py's shape for its own sibling
sweep-enqueue function: same dedup-key idempotency, same "not called
from inside heartbeat()" guard, same _run_job dispatch check.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import scheduler
from app.db.models import Job, PlannerCredential, PlannerSnapshot, UserState
from app.planner.jobs import PLANNER_SYNC
from app.worker import _run_job

pytestmark = pytest.mark.asyncio

TZ = "Europe/Paris"


def _settings(**overrides) -> Settings:
    base = dict(TZ_DEFAULT=TZ, PLANNER_ENABLED=True, PLANNER_SNAPSHOT_MAX_AGE_MIN=30)
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def _seed_state(session, *, timezone: str = TZ, chat_id: int = 4242) -> None:
    session.add(UserState(id=1, chat_id=chat_id, timezone=timezone))
    await session.commit()


async def _seed_credential(session, clock, *, enabled: bool = True, status: str = "active") -> None:
    session.add(
        PlannerCredential(
            id=1,
            access_token="at",
            refresh_token="rt",
            expires_at=clock.now_utc() + datetime.timedelta(hours=1),
            status=status,
            enabled=enabled,
        )
    )
    await session.commit()


async def test_no_enqueue_when_planner_disabled(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = _settings(PLANNER_ENABLED=False)
    async with sessionmaker() as session:
        await _seed_state(session)
        await _seed_credential(session, clock)
        enqueued = await scheduler.maybe_enqueue_planner_sync(session, settings, clock, TZ)
    assert enqueued is False


async def test_no_enqueue_before_linking(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = _settings()
    async with sessionmaker() as session:
        await _seed_state(session)
        enqueued = await scheduler.maybe_enqueue_planner_sync(session, settings, clock, TZ)
    assert enqueued is False


async def test_no_enqueue_when_user_paused(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = _settings()
    async with sessionmaker() as session:
        await _seed_state(session)
        await _seed_credential(session, clock, enabled=False)
        enqueued = await scheduler.maybe_enqueue_planner_sync(session, settings, clock, TZ)
    assert enqueued is False


async def test_enqueues_once_per_fifteen_minute_bucket(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 1, tz=TZ)
    settings = _settings()
    async with sessionmaker() as session:
        await _seed_state(session)
        await _seed_credential(session, clock)
        first = await scheduler.maybe_enqueue_planner_sync(session, settings, clock, TZ)
        second = await scheduler.maybe_enqueue_planner_sync(session, settings, clock, TZ)
    assert first is True
    assert second is False

    clock.advance(datetime.timedelta(minutes=15))
    async with sessionmaker() as session:
        third = await scheduler.maybe_enqueue_planner_sync(session, settings, clock, TZ)
    assert third is True

    async with sessionmaker() as session:
        rows = (await session.execute(select(Job).where(Job.kind == PLANNER_SYNC))).scalars().all()
    assert len(rows) == 2


async def test_a_premorning_sync_is_queued_on_its_own_key(sessionmaker, frozen_clock):
    settings = _settings(MORNING_TIME=datetime.time(9, 0))
    clock = frozen_clock(2026, 9, 23, 8, 50, tz=TZ)  # 10 minutes before 09:00
    async with sessionmaker() as session:
        await _seed_state(session)
        await _seed_credential(session, clock)
        enqueued = await scheduler.maybe_enqueue_planner_sync(session, settings, clock, TZ)
    assert enqueued is True

    async with sessionmaker() as session:
        rows = (await session.execute(select(Job).where(Job.kind == PLANNER_SYNC))).scalars().all()
    keys = {row.dedup_key for row in rows}
    assert any("premorning" in key for key in keys)


async def test_heartbeat_itself_never_enqueues_a_planner_sync(sessionmaker, frozen_clock):
    """Same guard as the research sweep's: heartbeat() is called
    directly and asserted against an exact job-table state by several
    other tests, so this must stay a sibling call in _heartbeat_loop."""
    clock = frozen_clock(2026, 9, 23, 15, 0, tz=TZ)
    settings = _settings()
    async with sessionmaker() as session:
        await _seed_state(session)
        await _seed_credential(session, clock)

    async with sessionmaker() as session:
        await scheduler.heartbeat(session, settings, clock)

    async with sessionmaker() as session:
        rows = (await session.execute(select(Job))).scalars().all()
    assert rows == []


async def test_worker_dispatches_planner_sync(sessionmaker, frozen_clock, fake_llm_provider):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = _settings()

    class _FakeClient:
        async def get_agenda(self, settings, session, clock, **kwargs):
            return {"events": [], "tasks": []}

    async with sessionmaker() as session:
        await _seed_state(session)
        await _seed_credential(session, clock)
        await _run_job(
            session, settings, None, fake_llm_provider, None, clock,
            PLANNER_SYNC, {}, None, _FakeClient(),
        )

    async with sessionmaker() as session:
        row = await session.get(PlannerSnapshot, 1)
    assert row is not None
    assert row.payload == {"events": [], "tasks": []}


async def test_worker_dispatch_raises_without_a_planner_client(sessionmaker, frozen_clock, fake_llm_provider):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = _settings()
    async with sessionmaker() as session:
        await _seed_state(session)
        with pytest.raises(ValueError):
            await _run_job(
                session, settings, None, fake_llm_provider, None, clock, PLANNER_SYNC, {},
            )
