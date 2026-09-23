"""Preemption: a user update mid-job -> the run reads `skipped:preempted`
with no partial writes it did not already commit (approved plan §5's
test list; approved plan §2's "Enforcement" section).

`run_idle` re-checks the gate and preemption right after claiming the
run, before the first backfill unit runs at all. A separate test drives
`app/core/idle/backfill.run_backfill` directly to prove the *between-
units* check: once a telegram_update lands, no further unit starts, and
whatever unit already committed stays exactly as committed (plan
section 2: "a preempted backfill keeps only whole units").
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle import BACKFILL
from app.core.idle.backfill import run_backfill
from app.core.idle.runner import run_idle
from app.db.models import IdleRun, Message, Scene, TelegramUpdate, UserState
from conftest import FakeLLMProvider

pytestmark = pytest.mark.asyncio


def _clock() -> FrozenClock:
    # Deliberately in the past relative to the real wall clock: is_preempted
    # compares TelegramUpdate.created_at (a real server_default=now())
    # against `started_at` (this frozen clock's "now"), so started_at must
    # be safely before whatever the real clock reads during the test run.
    return FrozenClock(datetime.datetime(2020, 1, 1, 12, 0, tzinfo=datetime.timezone.utc))


async def _seed_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="Europe/Paris"))
        await session.commit()


async def _make_ended_scene(sessionmaker, clock, *, summary=None) -> int:
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene = Scene(
            started_at=now - datetime.timedelta(hours=2),
            ended_at=now - datetime.timedelta(hours=1),
            summary=summary,
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


async def test_a_user_update_before_the_run_starts_skips_it_as_preempted(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _make_ended_scene(sessionmaker, clock, summary=None)

    async with sessionmaker() as session:
        run = IdleRun(kind=BACKFILL, local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    # A telegram_update lands right after the run would be claimed.
    # created_at is server_default=now() (the real wall clock), and
    # `started_at` (below, inside run_idle) is this frozen clock's own
    # "now" -- 2020-01-01, safely in the past -- so the comparison
    # `created_at > started_at` is unambiguously true.
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={}))
        await session.commit()

    provider = FakeLLMProvider(text="не должно быть вызвано")
    safety_provider = FakeLLMProvider()

    await run_idle(sessionmaker, Settings(), provider, safety_provider, clock, run_id=run_id)

    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        scene = (await session.execute(select(Scene))).scalars().first()

    assert run.status == "skipped"
    assert run.skip_reason == "preempted"
    assert scene.summary is None, "no content write must have committed"
    assert provider.calls == 0


async def test_backfill_stops_between_units_and_keeps_finished_ones(sessionmaker):
    """Two candidate scenes; a telegram_update appears after the first
    unit's own preemption check passes but before the second's."""
    clock = _clock()
    await _seed_state(sessionmaker)
    await _make_ended_scene(sessionmaker, clock, summary=None)
    await _make_ended_scene(sessionmaker, clock, summary=None)

    started_at = clock.now_utc()
    provider = FakeLLMProvider(text="сводка")
    safety_provider = FakeLLMProvider()

    original_complete = provider.complete

    async def _complete_then_insert(*args, **kwargs):
        # Simulate a user message arriving in between units: insert it
        # right after the first unit's own model call completes, so the
        # second unit's preemption check (right before it starts) sees it.
        result = await original_complete(*args, **kwargs)
        if provider.calls == 1:
            async with sessionmaker() as session:
                session.add(TelegramUpdate(update_id=1, payload={}))
                await session.commit()
        return result

    provider.complete = _complete_then_insert

    result = await run_backfill(
        sessionmaker, Settings(), provider, safety_provider, clock,
        started_at=started_at, timezone="Europe/Paris",
    )

    assert result.summarized == 1
    assert result.preempted is True

    async with sessionmaker() as session:
        scenes = {row.id: row.summary for row in (await session.execute(select(Scene))).scalars().all()}
    summarized_count = sum(1 for summary in scenes.values() if summary is not None)
    assert summarized_count == 1, "only the whole finished unit is kept"
