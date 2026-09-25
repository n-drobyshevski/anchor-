"""The `reflect` idle kind: the 7-day input, reuse of the Phase 5
validator and apply step, ownership, caps (including a cap-driven
close), welfare exclusion, undo and its kind rule (Phase 6 plan section
6.3; approved plan §5's test list for milestone 6b)."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import notebook as notebook_module
from app.core.clock import FrozenClock
from app.core.idle.reflect import (
    REFLECT_CATEGORY,
    has_new_summary_since,
    last_done_reflect_finished_at,
    run_reflect,
)
from app.core.idle.runner import RunContext
from app.core.idle.undo import undo_run
from app.db.models import IdleChange, IdleRun, Message, NotebookEntry, Scene, SpendLedger, UserState
from conftest import FakeLLMProvider

TIMEZONE = "Europe/Paris"


def _clock(**kwargs) -> FrozenClock:
    base = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc)
    return FrozenClock(base + datetime.timedelta(**kwargs) if kwargs else base)


def _settings(**overrides) -> Settings:
    base = {"DAILY_USD_CAP": 5.00}
    base.update(overrides)
    return Settings(**base)


async def _ensure_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        existing = await session.get(UserState, 1)
        if existing is None:
            session.add(UserState(id=1, chat_id=555, timezone=TIMEZONE))
            await session.commit()


async def _scene_with_summary(
    sessionmaker, clock, *, ended_hours_ago=1, summary="Коротко.", welfare=False
) -> int:
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene = Scene(
            started_at=now - datetime.timedelta(hours=ended_hours_ago, minutes=30),
            ended_at=now - datetime.timedelta(hours=ended_hours_ago),
            summary=summary,
        )
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        session.add_all(
            [
                Message(role="user", content="1", ooc=False, kind="chat", scene_id=scene.id),
                Message(role="assistant", content="2", ooc=False, kind="chat", scene_id=scene.id),
            ]
        )
        if welfare:
            session.add(
                Message(role="assistant", content="w", ooc=True, kind="welfare", scene_id=scene.id)
            )
        await session.commit()
        return scene.id


async def _idle_run(sessionmaker, clock, *, kind="reflect", status="queued") -> int:
    async with sessionmaker() as session:
        run = IdleRun(kind=kind, local_date=clock.now_utc().date(), status=status)
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


# --- has_new_summary_since(): welfare-excluded watermark -----------------


@pytest.mark.asyncio
async def test_has_new_summary_since_true_with_a_fresh_summary(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    await _scene_with_summary(sessionmaker, clock)
    async with sessionmaker() as session:
        assert await has_new_summary_since(session, None) is True


@pytest.mark.asyncio
async def test_has_new_summary_since_ignores_welfare_scenes(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    await _scene_with_summary(sessionmaker, clock, welfare=True)
    async with sessionmaker() as session:
        assert await has_new_summary_since(session, None) is False


@pytest.mark.asyncio
async def test_has_new_summary_since_respects_the_watermark(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    await _scene_with_summary(sessionmaker, clock, ended_hours_ago=5)
    watermark = clock.now_utc() - datetime.timedelta(hours=1)
    async with sessionmaker() as session:
        assert await has_new_summary_since(session, watermark) is False


@pytest.mark.asyncio
async def test_last_done_reflect_finished_at_only_counts_done_runs(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        session.add(IdleRun(kind="reflect", local_date=clock.now_utc().date(), status="skipped"))
        await session.commit()
    async with sessionmaker() as session:
        assert await last_done_reflect_finished_at(session) is None

    finished = clock.now_utc()
    async with sessionmaker() as session:
        session.add(
            IdleRun(
                kind="reflect", local_date=clock.now_utc().date(), status="done",
                finished_at=finished,
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        assert await last_done_reflect_finished_at(session) == finished


# --- run_reflect(): reuses notebook.validate and notebook.apply_plan -----


@pytest.mark.asyncio
async def test_run_reflect_adds_observations_and_ledgers(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    await _scene_with_summary(sessionmaker, clock)
    run_id = await _idle_run(sessionmaker, clock)

    safety_provider = FakeLLMProvider(
        text='{"add": [{"kind": "observation", "text": "Работает по вечерам."}], '
        '"close": [], "update": []}'
    )
    result = await run_reflect(
        sessionmaker, _settings(), safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )

    assert result.added == 1
    async with sessionmaker() as session:
        entries = (await session.execute(select(NotebookEntry))).scalars().all()
        assert len(entries) == 1
        assert entries[0].source == "anchor"
        assert entries[0].scene_id is None
        rows = (await session.execute(select(SpendLedger))).scalars().all()
        assert any(row.category == REFLECT_CATEGORY for row in rows)
        changes = (
            await session.execute(select(IdleChange).where(IdleChange.run_id == run_id))
        ).scalars().all()
        assert any(c.op == "insert" and c.table_name == "notebook_entry" for c in changes)


@pytest.mark.asyncio
async def test_run_reflect_never_adds_an_intention(sessionmaker):
    """Reuses notebook.validate, so this holds for the same reason it
    holds for the per-scene job: it is the same code."""
    clock = _clock()
    await _ensure_state(sessionmaker)
    await _scene_with_summary(sessionmaker, clock)
    run_id = await _idle_run(sessionmaker, clock)

    safety_provider = FakeLLMProvider(
        text='{"add": [{"kind": "intention", "text": "быть добрее"}], "close": [], "update": []}'
    )
    result = await run_reflect(
        sessionmaker, _settings(), safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )
    assert result.added == 0
    async with sessionmaker() as session:
        assert (await session.execute(select(NotebookEntry))).scalars().all() == []


@pytest.mark.asyncio
async def test_run_reflect_never_closes_a_user_entry(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    await _scene_with_summary(sessionmaker, clock)
    async with sessionmaker() as session:
        entry = NotebookEntry(kind="intention", text="бросить курить", source="user")
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        entry_id = entry.id
    run_id = await _idle_run(sessionmaker, clock)

    safety_provider = FakeLLMProvider(
        text=(
            '{"add": [], "close": [{"id": %d, "why": "resolved"}], "update": []}' % entry_id
        )
    )
    result = await run_reflect(
        sessionmaker, _settings(), safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )
    assert result.closed == 0
    async with sessionmaker() as session:
        assert (await session.get(NotebookEntry, entry_id)).active is True


@pytest.mark.asyncio
async def test_run_reflect_cap_driven_close_is_logged(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    await _scene_with_summary(sessionmaker, clock)
    async with sessionmaker() as session:
        oldest = NotebookEntry(kind="observation", text="старое наблюдение", source="anchor")
        session.add(oldest)
        await session.commit()
        await session.refresh(oldest)
        oldest_id = oldest.id
    run_id = await _idle_run(sessionmaker, clock)

    safety_provider = FakeLLMProvider(
        text='{"add": [{"kind": "observation", "text": "новое наблюдение о привычках"}], '
        '"close": [], "update": []}'
    )
    settings = _settings(NOTEBOOK_MAX_OBSERVATIONS=1)
    result = await run_reflect(
        sessionmaker, settings, safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )
    assert result.added == 1
    async with sessionmaker() as session:
        assert (await session.get(NotebookEntry, oldest_id)).active is False
        changes = (
            await session.execute(select(IdleChange).where(IdleChange.run_id == run_id))
        ).scalars().all()
        close_ops = [c for c in changes if c.op == "close" and c.row_id == oldest_id]
        assert len(close_ops) == 1


@pytest.mark.asyncio
async def test_run_reflect_welfare_scenes_excluded_from_input_and_kind_rule(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    await _scene_with_summary(sessionmaker, clock, welfare=True, summary="что-то деликатное")
    run_id = await _idle_run(sessionmaker, clock)

    safety_provider = FakeLLMProvider(text='{"add": [], "close": [], "update": []}')
    await run_reflect(
        sessionmaker, _settings(), safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )
    assert "деликатное" not in safety_provider.received_messages[0][1].content


# --- undo -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_restores_an_add_exactly(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    await _scene_with_summary(sessionmaker, clock)
    run_id = await _idle_run(sessionmaker, clock)

    safety_provider = FakeLLMProvider(
        text='{"add": [{"kind": "observation", "text": "Работает по вечерам."}], '
        '"close": [], "update": []}'
    )
    await run_reflect(
        sessionmaker, _settings(), safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        run.status = "done"
        run.reversible = True
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    assert result.skipped_conflicts == 0
    async with sessionmaker() as session:
        assert (await session.execute(select(NotebookEntry))).scalars().all() == []


@pytest.mark.asyncio
async def test_undo_reactivates_a_cap_driven_close(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    await _scene_with_summary(sessionmaker, clock)
    async with sessionmaker() as session:
        oldest = NotebookEntry(kind="observation", text="старое наблюдение", source="anchor")
        session.add(oldest)
        await session.commit()
        await session.refresh(oldest)
        oldest_id = oldest.id
    run_id = await _idle_run(sessionmaker, clock)

    safety_provider = FakeLLMProvider(
        text='{"add": [{"kind": "observation", "text": "новое наблюдение о привычках"}], '
        '"close": [], "update": []}'
    )
    settings = _settings(NOTEBOOK_MAX_OBSERVATIONS=1)
    await run_reflect(
        sessionmaker, settings, safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone=TIMEZONE,
    )
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        run.status = "done"
        run.reversible = True
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    async with sessionmaker() as session:
        # The cap-driven close is reversed along with the insert it made room for.
        assert (await session.get(NotebookEntry, oldest_id)).active is True
        assert (await session.execute(select(NotebookEntry))).scalars().all() == [
            (await session.get(NotebookEntry, oldest_id))
        ]
