"""The `consolidate` idle kind: clustering, the pure validator, never
touching protected memories, apply, undo and its kind rule (Phase 6
plan section 6.2; approved plan §5's test list for milestone 6b)."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle.consolidate import (
    CONSOLIDATE_CATEGORY,
    ConsolidatePlan,
    apply_consolidate,
    find_clusters,
    run_consolidate,
    validate,
)
from app.core.idle.runner import RunContext
from app.core.idle.undo import undo_run
from app.db.models import IdleChange, IdleRun, Memory, SpendLedger, UserState
from conftest import FakeLLMProvider

def _clock(**kwargs) -> FrozenClock:
    base = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc)
    return FrozenClock(base + datetime.timedelta(**kwargs) if kwargs else base)


async def _ensure_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        existing = await session.get(UserState, 1)
        if existing is None:
            session.add(UserState(id=1, chat_id=555, timezone="Europe/Paris"))
            await session.commit()


async def _memory(sessionmaker, *, kind="event", text, source="extractor", pinned=False) -> int:
    async with sessionmaker() as session:
        memory = Memory(kind=kind, text=text, source=source, pinned=pinned)
        session.add(memory)
        await session.commit()
        await session.refresh(memory)
        return memory.id


async def _idle_run(sessionmaker, clock, *, kind="consolidate", status="queued") -> int:
    async with sessionmaker() as session:
        run = IdleRun(kind=kind, local_date=clock.now_utc().date(), status=status)
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


# --- validate(): pure, trusts nothing -----------------------------------


def test_validate_drops_ids_not_in_candidate_set():
    payload = {"merges": [{"ids": [1, 99], "text": "т", "kind": "event"}], "contradictions": []}
    plan = validate(payload, candidate_ids={1, 2, 3})
    assert plan.merges == []  # 99 isn't a candidate, and a stripped [1] has < 2 ids


def test_validate_merge_needs_at_least_two_ids():
    payload = {"merges": [{"ids": [1], "text": "т", "kind": "event"}], "contradictions": []}
    plan = validate(payload, candidate_ids={1, 2})
    assert plan.merges == []


def test_validate_rejects_bad_kind():
    payload = {"merges": [{"ids": [1, 2], "text": "т", "kind": "rule"}], "contradictions": []}
    plan = validate(payload, candidate_ids={1, 2})
    assert plan.merges == []


def test_validate_rejects_text_over_300_chars():
    payload = {
        "merges": [{"ids": [1, 2], "text": "т" * 301, "kind": "event"}],
        "contradictions": [],
    }
    plan = validate(payload, candidate_ids={1, 2})
    assert plan.merges == []


def test_validate_drops_merge_on_any_screen_failure_including_intensity():
    payload = {
        "merges": [
            {"ids": [1, 2], "text": "Надо быть строже к себе и без поблажек.", "kind": "event"}
        ],
        "contradictions": [],
    }
    plan = validate(payload, candidate_ids={1, 2})
    assert plan.merges == []  # dropped, no risk_intensity carve-out unlike /mind add


def test_validate_contradiction_keep_must_differ_from_drop():
    payload = {"merges": [], "contradictions": [{"keep_id": 1, "drop_id": 1}]}
    plan = validate(payload, candidate_ids={1, 2})
    assert plan.contradictions == []


def test_validate_contradiction_ids_must_be_candidates():
    payload = {"merges": [], "contradictions": [{"keep_id": 1, "drop_id": 99}]}
    plan = validate(payload, candidate_ids={1, 2})
    assert plan.contradictions == []


def test_validate_an_id_is_used_at_most_once_across_the_whole_payload():
    """id 2 is named by the merge first (payload order); the
    contradiction that reuses it is dropped, not the merge."""
    payload = {
        "merges": [{"ids": [1, 2], "text": "т", "kind": "event"}],
        "contradictions": [{"keep_id": 2, "drop_id": 3}],
    }
    plan = validate(payload, candidate_ids={1, 2, 3})
    assert plan.merges == [{"ids": [1, 2], "text": "т", "kind": "event"}]
    assert plan.contradictions == []


def test_validate_accepts_a_clean_payload():
    payload = {
        "merges": [{"ids": [1, 2], "text": "живёт в Лилле", "kind": "identity"}],
        "contradictions": [{"keep_id": 3, "drop_id": 4}],
    }
    plan = validate(payload, candidate_ids={1, 2, 3, 4})
    assert plan == ConsolidatePlan(
        merges=[{"ids": [1, 2], "text": "живёт в Лилле", "kind": "identity"}],
        contradictions=[{"keep_id": 3, "drop_id": 4}],
    )


# --- find_clusters(): never selects protected memories ------------------


@pytest.mark.asyncio
async def test_find_clusters_never_selects_protected_memories(sessionmaker):
    await _ensure_state(sessionmaker)
    text = "пользователь живёт в Лилле и работает инженером"
    similar_text = "пользователь живёт в Лилле, работает инженером"
    await _memory(sessionmaker, text=text, source="extractor")
    await _memory(sessionmaker, text=similar_text, source="extractor")
    protected_ids = [
        await _memory(sessionmaker, text=similar_text, source="user"),
        await _memory(sessionmaker, text=similar_text, source="adopt"),
        await _memory(sessionmaker, text=similar_text, source="extractor", pinned=True),
        await _memory(sessionmaker, kind="rule", text=similar_text, source="extractor"),
        await _memory(sessionmaker, kind="technique", text=similar_text, source="extractor"),
    ]

    async with sessionmaker() as session:
        clusters = await find_clusters(session)

    clustered_ids = {i for cluster in clusters for i in cluster}
    assert not clustered_ids & set(protected_ids)
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


@pytest.mark.asyncio
async def test_find_clusters_fewer_than_two_similar_is_no_cluster(sessionmaker):
    await _ensure_state(sessionmaker)
    await _memory(sessionmaker, text="пользователь любит кофе", source="extractor")
    await _memory(sessionmaker, text="пользователь не любит спорт вообще никогда", source="extractor")

    async with sessionmaker() as session:
        clusters = await find_clusters(session)
    assert clusters == []


# --- apply_consolidate(): re-checks at apply time, supersede chains -----


@pytest.mark.asyncio
async def test_apply_consolidate_merge_inserts_and_supersedes(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    id1 = await _memory(sessionmaker, text="живёт в Лилле")
    id2 = await _memory(sessionmaker, text="живёт в Лилле, во Франции")
    run_id = await _idle_run(sessionmaker, clock)

    plan = ConsolidatePlan(merges=[{"ids": [id1, id2], "text": "живёт в Лилле", "kind": "identity"}])
    async with sessionmaker() as session:
        ctx = RunContext(
            session=session, settings=Settings(), clock=clock, run_id=run_id,
            kind="consolidate", started_at=clock.now_utc(), timezone="Europe/Paris",
        )
        result = await apply_consolidate(session, ctx, plan)
        await session.commit()

    assert result.merged == 1
    async with sessionmaker() as session:
        m1 = await session.get(Memory, id1)
        m2 = await session.get(Memory, id2)
        assert m1.superseded_by == m2.superseded_by
        new_id = m1.superseded_by
        new_memory = await session.get(Memory, new_id)
        assert new_memory.source == "consolidate"
        assert new_memory.text == "живёт в Лилле"

        changes = (
            await session.execute(select(IdleChange).where(IdleChange.run_id == run_id))
        ).scalars().all()
        ops = {(c.table_name, c.row_id, c.op) for c in changes}
        assert ("memory", new_id, "insert") in ops
        assert ("memory", id1, "supersede") in ops
        assert ("memory", id2, "supersede") in ops


@pytest.mark.asyncio
async def test_apply_consolidate_never_writes_a_protected_row_even_if_named(sessionmaker):
    """A merge naming a protected id (smuggled past validate() somehow,
    or protected by a race between candidate-building and apply) is
    dropped whole at apply time -- the second of the module's two
    enforcement points."""
    clock = _clock()
    await _ensure_state(sessionmaker)
    ok_id = await _memory(sessionmaker, text="живёт в Лилле")
    pinned_id = await _memory(sessionmaker, text="живёт в Лилле, во Франции", pinned=True)
    run_id = await _idle_run(sessionmaker, clock)

    plan = ConsolidatePlan(
        merges=[{"ids": [ok_id, pinned_id], "text": "живёт в Лилле", "kind": "identity"}]
    )
    async with sessionmaker() as session:
        ctx = RunContext(
            session=session, settings=Settings(), clock=clock, run_id=run_id,
            kind="consolidate", started_at=clock.now_utc(), timezone="Europe/Paris",
        )
        result = await apply_consolidate(session, ctx, plan)
        await session.commit()

    assert result.merged == 0
    assert result.dropped == 1
    async with sessionmaker() as session:
        assert (await session.get(Memory, pinned_id)).superseded_by is None
        assert (await session.get(Memory, ok_id)).superseded_by is None


@pytest.mark.asyncio
async def test_apply_consolidate_contradiction_supersedes_drop_with_keep(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    keep_id = await _memory(sessionmaker, text="живёт в Лилле")
    drop_id = await _memory(sessionmaker, text="живёт в Руане")
    run_id = await _idle_run(sessionmaker, clock)

    plan = ConsolidatePlan(contradictions=[{"keep_id": keep_id, "drop_id": drop_id}])
    async with sessionmaker() as session:
        ctx = RunContext(
            session=session, settings=Settings(), clock=clock, run_id=run_id,
            kind="consolidate", started_at=clock.now_utc(), timezone="Europe/Paris",
        )
        result = await apply_consolidate(session, ctx, plan)
        await session.commit()

    assert result.contradicted == 1
    async with sessionmaker() as session:
        assert (await session.get(Memory, drop_id)).superseded_by == keep_id
        assert (await session.get(Memory, keep_id)).superseded_by is None


# --- run_consolidate(): end to end with a scripted FakeLLMProvider ------


@pytest.mark.asyncio
async def test_run_consolidate_end_to_end_ledgers_and_writes(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    id1 = await _memory(sessionmaker, text="живёт в Лилле")
    id2 = await _memory(sessionmaker, text="живёт в Лилле, во Франции")
    run_id = await _idle_run(sessionmaker, clock)

    safety_provider = FakeLLMProvider(
        text=(
            '{"merges": [{"ids": [%d, %d], "text": "живёт в Лилле", "kind": "identity"}], '
            '"contradictions": []}' % (id1, id2)
        )
    )

    result = await run_consolidate(
        sessionmaker, Settings(), safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone="Europe/Paris",
    )

    assert result.merged == 1
    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
        assert any(row.category == CONSOLIDATE_CATEGORY for row in rows)


@pytest.mark.asyncio
async def test_run_consolidate_no_clusters_is_a_clean_no_op(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    run_id = await _idle_run(sessionmaker, clock)
    safety_provider = FakeLLMProvider(text='{"merges": [], "contradictions": []}')

    result = await run_consolidate(
        sessionmaker, Settings(), safety_provider, clock,
        run_id=run_id, started_at=clock.now_utc(), timezone="Europe/Paris",
    )
    assert result.merged == 0
    assert result.contradicted == 0


# --- undo -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_restores_a_merge_exactly(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    id1 = await _memory(sessionmaker, text="живёт в Лилле")
    id2 = await _memory(sessionmaker, text="живёт в Лилле, во Франции")
    run_id = await _idle_run(sessionmaker, clock)

    plan = ConsolidatePlan(merges=[{"ids": [id1, id2], "text": "живёт в Лилле", "kind": "identity"}])
    async with sessionmaker() as session:
        ctx = RunContext(
            session=session, settings=Settings(), clock=clock, run_id=run_id,
            kind="consolidate", started_at=clock.now_utc(), timezone="Europe/Paris",
        )
        await apply_consolidate(session, ctx, plan)
        run = await session.get(IdleRun, run_id)
        run.status = "done"
        run.reversible = True
        await session.commit()

    async with sessionmaker() as session:
        new_id = (await session.get(Memory, id1)).superseded_by
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    assert result.skipped_conflicts == 0
    async with sessionmaker() as session:
        assert await session.get(Memory, new_id) is None
        assert (await session.get(Memory, id1)).superseded_by is None
        assert (await session.get(Memory, id2)).superseded_by is None


@pytest.mark.asyncio
async def test_undo_reports_partial_conflict_when_a_row_changed_since(sessionmaker):
    clock = _clock()
    await _ensure_state(sessionmaker)
    id1 = await _memory(sessionmaker, text="живёт в Лилле")
    id2 = await _memory(sessionmaker, text="живёт в Лилле, во Франции")
    run_id = await _idle_run(sessionmaker, clock)

    plan = ConsolidatePlan(merges=[{"ids": [id1, id2], "text": "живёт в Лилле", "kind": "identity"}])
    async with sessionmaker() as session:
        ctx = RunContext(
            session=session, settings=Settings(), clock=clock, run_id=run_id,
            kind="consolidate", started_at=clock.now_utc(), timezone="Europe/Paris",
        )
        await apply_consolidate(session, ctx, plan)
        run = await session.get(IdleRun, run_id)
        run.status = "done"
        run.reversible = True
        await session.commit()

    # Something else touches id1 after the idle run -- e.g. the user pins it.
    async with sessionmaker() as session:
        memory = await session.get(Memory, id1)
        memory.pinned = True
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    assert result.skipped_conflicts >= 1
    async with sessionmaker() as session:
        # The conflicting row (id1) keeps its overwritten state.
        assert (await session.get(Memory, id1)).pinned is True
