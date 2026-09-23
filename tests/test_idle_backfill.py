"""The `backfill` idle kind: summarizes and reflects pending scenes,
ledgers as `idle:backfill`, and skips welfare scenes (approved plan §5's
test list for milestone 6a)."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle.backfill import IDLE_BACKFILL_CATEGORY, run_backfill
from app.db.models import NotebookEntry, Scene, SpendLedger, UserState
from conftest import FakeLLMProvider

pytestmark = pytest.mark.asyncio


def _clock() -> FrozenClock:
    return FrozenClock(datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc))


async def _ensure_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        existing = await session.get(UserState, 1)
        if existing is None:
            session.add(UserState(id=1, chat_id=555, timezone="Europe/Paris"))
            await session.commit()


async def _make_ended_scene(sessionmaker, clock, *, summary=None, welfare=False) -> int:
    from app.db.models import Message

    await _ensure_state(sessionmaker)
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
        if welfare:
            session.add(
                Message(role="assistant", content="w", ooc=True, kind="welfare", scene_id=scene.id)
            )
        await session.commit()
        return scene.id


async def test_summarizes_a_pending_scene_and_ledgers_as_idle_backfill(sessionmaker):
    clock = _clock()
    scene_id = await _make_ended_scene(sessionmaker, clock, summary=None)

    provider = FakeLLMProvider(text="Коротко поговорили.")
    safety_provider = FakeLLMProvider(text='{"add": [], "close": [], "update": []}')

    result = await run_backfill(
        sessionmaker, Settings(), provider, safety_provider, clock,
        started_at=clock.now_utc() - datetime.timedelta(minutes=1), timezone="Europe/Paris",
    )

    assert result.summarized == 1
    async with sessionmaker() as session:
        scene = await session.get(Scene, scene_id)
        assert scene.summary == "Коротко поговорили."
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    assert any(row.category == IDLE_BACKFILL_CATEGORY for row in rows)


async def test_reflects_a_summarized_scene_with_no_reflection_yet(sessionmaker):
    clock = _clock()
    scene_id = await _make_ended_scene(sessionmaker, clock, summary="Уже есть сводка.")

    provider = FakeLLMProvider(text="не используется")
    safety_provider = FakeLLMProvider(
        text='{"add": [{"kind": "observation", "text": "Наблюдение."}], "close": [], "update": []}'
    )

    result = await run_backfill(
        sessionmaker, Settings(), provider, safety_provider, clock,
        started_at=clock.now_utc() - datetime.timedelta(minutes=1), timezone="Europe/Paris",
    )

    assert result.reflected == 1
    async with sessionmaker() as session:
        entries = (
            await session.execute(select(NotebookEntry).where(NotebookEntry.scene_id == scene_id))
        ).scalars().all()
    assert len(entries) == 1
    assert entries[0].text == "Наблюдение."


async def test_skips_welfare_scenes_for_reflection(sessionmaker):
    clock = _clock()
    await _make_ended_scene(sessionmaker, clock, summary="Есть сводка.", welfare=True)

    provider = FakeLLMProvider()
    safety_provider = FakeLLMProvider(
        text='{"add": [{"kind": "observation", "text": "не должно появиться"}], "close": [], "update": []}'
    )

    result = await run_backfill(
        sessionmaker, Settings(), provider, safety_provider, clock,
        started_at=clock.now_utc() - datetime.timedelta(minutes=1), timezone="Europe/Paris",
    )

    assert result.reflected == 0
    assert safety_provider.calls == 0
    async with sessionmaker() as session:
        entries = (await session.execute(select(NotebookEntry))).scalars().all()
    assert entries == []


async def test_caps_at_three_units_per_run(sessionmaker):
    clock = _clock()
    for _ in range(5):
        await _make_ended_scene(sessionmaker, clock, summary=None)

    provider = FakeLLMProvider(text="сводка")
    safety_provider = FakeLLMProvider(text='{"add": [], "close": [], "update": []}')

    result = await run_backfill(
        sessionmaker, Settings(), provider, safety_provider, clock,
        started_at=clock.now_utc() - datetime.timedelta(minutes=1), timezone="Europe/Paris",
    )

    assert result.summarized + result.reflected <= 3


async def test_nothing_to_backfill_is_a_no_op(sessionmaker):
    clock = _clock()
    provider = FakeLLMProvider()
    safety_provider = FakeLLMProvider()

    result = await run_backfill(
        sessionmaker, Settings(), provider, safety_provider, clock,
        started_at=clock.now_utc(), timezone="Europe/Paris",
    )

    assert result.summarized == 0
    assert result.reflected == 0
    assert result.preempted is False
    assert provider.calls == 0
    assert safety_provider.calls == 0


async def test_an_empty_reflection_is_not_paid_for_twice(sessionmaker):
    """A reflection that correctly adds nothing leaves no notebook row;
    the run's summary is what stops the next run picking it again."""
    from app.core.idle.candidates import REFLECTED_SCENE_IDS, reflect_candidates
    from app.db.models import IdleRun

    clock = _clock()
    scene_id = await _make_ended_scene(sessionmaker, clock, summary="Есть сводка.")
    safety_provider = FakeLLMProvider(text='{"add": [], "close": [], "update": []}')

    result = await run_backfill(
        sessionmaker, Settings(), FakeLLMProvider(), safety_provider, clock,
        started_at=clock.now_utc() - datetime.timedelta(minutes=1), timezone="Europe/Paris",
    )
    assert result.reflected_scene_ids == (scene_id,)

    async with sessionmaker() as session:
        session.add(
            IdleRun(kind="backfill", local_date=datetime.date(2026, 9, 23), status="done",
                    summary={REFLECTED_SCENE_IDS: [scene_id]})
        )
        await session.commit()
    async with sessionmaker() as session:
        assert await reflect_candidates(session, clock, 3) == []


async def test_a_scene_whose_live_reflect_job_ran_is_not_a_candidate(sessionmaker):
    from app.core.idle.candidates import reflect_candidates
    from app.db.models import Job

    clock = _clock()
    scene_id = await _make_ended_scene(sessionmaker, clock, summary="Есть сводка.")
    async with sessionmaker() as session:
        job = Job(kind="notebook_reflect", payload={"scene_id": scene_id},
                  dedup_key=f"nb:{scene_id}", status="done")
        session.add(job)
        await session.commit()
        assert await reflect_candidates(session, clock, 3) == []
        job.status = "failed"
        await session.commit()
        assert await reflect_candidates(session, clock, 3) == [scene_id]


async def test_stops_at_the_per_job_cap(sessionmaker):
    clock = _clock()
    for _ in range(3):
        await _make_ended_scene(sessionmaker, clock, summary=None)
    started_at = clock.now_utc() - datetime.timedelta(minutes=1)
    async with sessionmaker() as session:
        session.add(
            SpendLedger(local_date=datetime.date(2026, 9, 23), category=IDLE_BACKFILL_CATEGORY,
                        model="m", tokens_in=0, tokens_out=0, usd_cost=0.05,
                        ts=clock.now_utc())
        )
        await session.commit()

    provider = FakeLLMProvider(text="сводка")
    result = await run_backfill(
        sessionmaker, Settings(), provider, FakeLLMProvider(), clock,
        started_at=started_at, timezone="Europe/Paris",
    )

    assert result.job_cap is True
    assert provider.calls == 0
