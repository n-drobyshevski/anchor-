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
from app.core.idle import BACKFILL, CONSOLIDATE, REFLECT
from app.core.idle.backfill import run_backfill
from app.core.idle.consolidate import run_consolidate
from app.core.idle.reflect import run_reflect
from app.core.idle.runner import run_idle
from app.db.models import (
    IdleRun,
    Memory,
    Message,
    NotebookEntry,
    Scene,
    SpendLedger,
    TelegramUpdate,
    UserState,
)
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


# --- 6b: consolidate and reflect are single-transaction, so "preempted"
# always means nothing was written at all ------------------------------


async def _seed_consolidate_candidates(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add_all(
            [
                Memory(kind="identity", text="живёт в Лилле", source="extractor"),
                Memory(kind="identity", text="живёт в Лилле, во Франции", source="extractor"),
            ]
        )
        await session.commit()


async def _seed_two_consolidate_clusters(sessionmaker) -> None:
    """Two clusters (KIND_DAILY_MAX-independent) so the outer gate
    (row 10, `kind_rule:not_enough_clusters` needs >= 2 clusters) passes
    and `run_idle`'s own preemption re-check is actually reached."""
    async with sessionmaker() as session:
        session.add_all(
            [
                Memory(kind="identity", text="живёт в Лилле", source="extractor"),
                Memory(kind="identity", text="живёт в Лилле, во Франции", source="extractor"),
                Memory(kind="preference", text="работает программистом", source="extractor"),
                Memory(kind="preference", text="работает программистом в стартапе", source="extractor"),
            ]
        )
        await session.commit()


async def test_consolidate_before_the_run_starts_is_preempted_with_no_writes(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_two_consolidate_clusters(sessionmaker)

    async with sessionmaker() as session:
        run = IdleRun(kind=CONSOLIDATE, local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=1, payload={}))
        await session.commit()

    safety_provider = FakeLLMProvider(text='{"merges": [], "contradictions": []}')
    await run_idle(sessionmaker, Settings(), safety_provider, safety_provider, clock, run_id=run_id)

    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        assert run.status == "skipped"
        assert run.skip_reason == "preempted"
        memories = (await session.execute(select(Memory))).scalars().all()
        assert all(m.superseded_by is None for m in memories)
    assert safety_provider.calls == 0


async def test_consolidate_preempted_between_model_call_and_apply_writes_nothing(sessionmaker):
    """The model call already ran (and is ledgered), but a user update
    lands before the apply transaction opens -- no memory write must
    land."""
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_consolidate_candidates(sessionmaker)

    async with sessionmaker() as session:
        ids = [row.id for row in (await session.execute(select(Memory))).scalars().all()]

    async with sessionmaker() as session:
        run = IdleRun(kind=CONSOLIDATE, local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    safety_provider = FakeLLMProvider(
        text=(
            '{"merges": [{"ids": [%d, %d], "text": "живёт в Лилле", "kind": "identity"}], '
            '"contradictions": []}' % tuple(ids)
        )
    )

    async def _insert_update_after_call(*args, **kwargs):
        result = await FakeLLMProvider.complete(safety_provider, *args, **kwargs)
        async with sessionmaker() as session:
            session.add(TelegramUpdate(update_id=1, payload={}))
            await session.commit()
        return result

    safety_provider.complete = _insert_update_after_call

    result = await run_consolidate(
        sessionmaker, Settings(), safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone="Europe/Paris",
    )

    assert result.preempted is True
    assert result.merged == 0
    async with sessionmaker() as session:
        memories = (await session.execute(select(Memory))).scalars().all()
        assert all(m.superseded_by is None for m in memories)
        # money already spent is still ledgered (plan section 2).
        rows = (await session.execute(select(SpendLedger))).scalars().all()
        assert any(row.category == "idle:consolidate" for row in rows)


async def _seed_reflect_scene(sessionmaker, clock) -> None:
    now = clock.now_utc()
    async with sessionmaker() as session:
        scene = Scene(
            started_at=now - datetime.timedelta(hours=2),
            ended_at=now - datetime.timedelta(hours=1),
            summary="Коротко.",
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
        await session.commit()


async def test_reflect_preempted_between_model_call_and_apply_writes_nothing(sessionmaker):
    clock = _clock()
    await _seed_state(sessionmaker)
    await _seed_reflect_scene(sessionmaker, clock)

    async with sessionmaker() as session:
        run = IdleRun(kind=REFLECT, local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    safety_provider = FakeLLMProvider(
        text='{"add": [{"kind": "observation", "text": "Работает по вечерам."}], '
        '"close": [], "update": []}'
    )

    async def _insert_update_after_call(*args, **kwargs):
        result = await FakeLLMProvider.complete(safety_provider, *args, **kwargs)
        async with sessionmaker() as session:
            session.add(TelegramUpdate(update_id=1, payload={}))
            await session.commit()
        return result

    safety_provider.complete = _insert_update_after_call

    result = await run_reflect(
        sessionmaker, Settings(), safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone="Europe/Paris",
    )

    assert result.preempted is True
    assert result.added == 0
    async with sessionmaker() as session:
        assert (await session.execute(select(NotebookEntry))).scalars().all() == []
        rows = (await session.execute(select(SpendLedger))).scalars().all()
        assert any(row.category == "idle:reflect" for row in rows)


# --- Phase-6 package D: prebrief, critique and canary preempted mid-run -----


def _update_after(provider, sessionmaker):
    """Make `provider.complete` insert a telegram_update once it returns,
    i.e. the user writes while the model call is in flight."""

    async def _complete(*args, **kwargs):
        result = await FakeLLMProvider.complete(provider, *args, **kwargs)
        async with sessionmaker() as session:
            if await session.get(TelegramUpdate, 1) is None:
                session.add(TelegramUpdate(update_id=1, payload={}))
                await session.commit()
        return result

    provider.complete = _complete


async def _queued_run(sessionmaker, clock, kind) -> int:
    async with sessionmaker() as session:
        run = IdleRun(kind=kind, local_date=clock.now_utc().date(), status="queued")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


async def test_prebrief_preempted_mid_run_stores_no_note(sessionmaker):
    from app.core.idle.prebrief import run_prebrief
    from app.db.models import BriefNote, StandingOrder

    clock = _clock()
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        session.add(StandingOrder(text="выпить воды", cadence="daily", status="active", source="user"))
        await session.commit()
    run_id = await _queued_run(sessionmaker, clock, "prebrief")
    provider = FakeLLMProvider(text='{"notes": ["Вчера был тяжёлый день."]}')
    _update_after(provider, sessionmaker)

    result = await run_prebrief(
        sessionmaker, Settings(), provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone="Europe/Paris",
    )

    assert result.preempted is True
    async with sessionmaker() as session:
        assert (await session.execute(select(BriefNote))).scalars().all() == []


async def test_critique_preempted_mid_run_reports_preempted(sessionmaker):
    from app.core.idle.critique import run_critique

    clock = _clock()
    await _seed_state(sessionmaker)
    await _make_ended_scene(sessionmaker, clock, summary="Коротко.")
    run_id = await _queued_run(sessionmaker, clock, "critique")
    scores = '{"voice": 5, "one_action": 5, "boundaries": 5, "no_pressure": 5}'
    judge = FakeLLMProvider(text=scores)
    _update_after(judge, sessionmaker)

    result = await run_critique(
        sessionmaker, Settings(CRITIQUE_SAMPLE=5), clock,
        run_id=run_id, started_at=clock.now_utc(), timezone="Europe/Paris",
        judge_provider=judge,
    )

    assert result.preempted is True
    assert result.count == 0


async def test_canary_preempted_mid_run_keeps_no_case_results(sessionmaker, monkeypatch):
    from app.core.idle.canary import run_canary

    clock = _clock()
    await _seed_state(sessionmaker)
    run_id = await _queued_run(sessionmaker, clock, "canary")

    async def _trial(settings, *, clock, amendments, on_case_done=None, **kwargs):
        from eval.trial import TrialResult

        async with sessionmaker() as session:
            session.add(TelegramUpdate(update_id=1, payload={}))
            await session.commit()
        return TrialResult(cases={"04": True}, passed=True, usd_cost=0.01)

    monkeypatch.setattr("eval.trial.run_blocking_subset", _trial)

    result = await run_canary(
        sessionmaker, Settings(), clock,
        run_id=run_id, started_at=clock.now_utc(), timezone="Europe/Paris",
    )

    assert result.preempted is True
    assert result.cases == {}
    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    assert any(row.category == "idle:canary" for row in rows)  # money spent stays ledgered


# --- Phase-6 package D: a failed run says where, never what -----------------


def test_failure_site_names_our_innermost_frame_not_the_message():
    from app.core.idle.runner import failure_site

    def _inner():
        raise AttributeError("secret user text in the message")

    try:
        _inner()
    except AttributeError as exc:
        site = failure_site(exc)

    assert site.startswith("tests.test_idle_preemption:_inner:")
    assert "secret" not in site


async def test_a_failed_run_logs_the_site(sessionmaker, monkeypatch):
    import app.core.idle.runner as runner

    clock = _clock()
    await _seed_state(sessionmaker)
    await _make_ended_scene(sessionmaker, clock, summary=None)
    run_id = await _queued_run(sessionmaker, clock, BACKFILL)

    async def _boom(*args, **kwargs):
        raise AttributeError("never logged")

    monkeypatch.setattr("app.core.idle.backfill.run_backfill", _boom)
    logged = []
    monkeypatch.setattr(
        runner.logger, "warning", lambda msg, *a, **kw: logged.append((msg, kw.get("extra", {})))
    )

    await run_idle(sessionmaker, Settings(), FakeLLMProvider(), FakeLLMProvider(), clock, run_id=run_id)

    [(msg, extra)] = [entry for entry in logged if entry[0] == "idle run failed"]
    assert extra["event"] == "AttributeError"
    assert extra["where"].startswith("tests.test_idle_preemption:_boom:")
    assert "never logged" not in str(extra)
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
    assert (run.status, run.skip_reason) == ("failed", "AttributeError")
