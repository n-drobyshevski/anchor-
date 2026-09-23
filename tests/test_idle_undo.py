"""The undo engine: exact restore of each op, partial-conflict report,
a single transaction, and the age/status/reversibility refusals
(approved plan §5's test list for milestone 6a).

6a tests this with synthetic idle_change rows on `memory` and
`notebook_entry`; the real writers (consolidate, reflect) arrive in 6b.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.export import encode
from app.core.idle.undo import (
    REFUSAL_ALREADY_UNDONE,
    REFUSAL_NOT_DONE,
    REFUSAL_NOT_REVERSIBLE,
    REFUSAL_TOO_OLD,
    undo_run,
)
from app.db.models import IdleChange, IdleRun, Memory, NotebookEntry, StateChange

pytestmark = pytest.mark.asyncio


def _clock(**kwargs) -> FrozenClock:
    base = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.timezone.utc)
    return FrozenClock(base + datetime.timedelta(**kwargs) if kwargs else base)


def _row_state(row) -> dict:
    return {c.name: encode(getattr(row, c.name)) for c in row.__table__.columns}


async def _reversible_done_run(sessionmaker, clock) -> int:
    async with sessionmaker() as session:
        run = IdleRun(
            kind="backfill", local_date=clock.now_utc().date(), status="done", reversible=True,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


async def test_undo_insert_deletes_the_row(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        memory = Memory(kind="event", text="факт", source="consolidate")
        session.add(memory)
        await session.commit()
        await session.refresh(memory)
        after = _row_state(memory)
        memory_id = memory.id

    run_id = await _reversible_done_run(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add(
            IdleChange(run_id=run_id, table_name="memory", row_id=memory_id, op="insert", after=after)
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    assert result.restored == 1
    assert result.skipped_conflicts == 0
    async with sessionmaker() as session:
        assert await session.get(Memory, memory_id) is None
        run = await session.get(IdleRun, run_id)
        assert run.status == "undone"
        assert run.undone_at is not None


async def test_undo_close_reactivates_the_entry(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        entry = NotebookEntry(kind="open_thread", text="тема", source="anchor")
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        before = _row_state(entry)
        entry.active = False
        entry.closed_by = "anchor"
        entry.closed_at = clock.now_utc()
        await session.commit()
        await session.refresh(entry)
        after = _row_state(entry)
        entry_id = entry.id

    run_id = await _reversible_done_run(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add(
            IdleChange(
                run_id=run_id, table_name="notebook_entry", row_id=entry_id,
                op="close", before=before, after=after,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    assert result.restored == 1
    async with sessionmaker() as session:
        entry = await session.get(NotebookEntry, entry_id)
        assert entry.active is True
        assert entry.closed_by is None
        assert entry.closed_at is None


async def test_undo_update_restores_before_exactly(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        entry = NotebookEntry(kind="observation", text="старый текст", source="anchor")
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        before = _row_state(entry)
        entry.text = "новый текст"
        entry.updated_at = clock.now_utc()
        await session.commit()
        await session.refresh(entry)
        after = _row_state(entry)
        entry_id = entry.id

    run_id = await _reversible_done_run(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add(
            IdleChange(
                run_id=run_id, table_name="notebook_entry", row_id=entry_id,
                op="update", before=before, after=after,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    async with sessionmaker() as session:
        entry = await session.get(NotebookEntry, entry_id)
        assert entry.text == "старый текст"


async def test_undo_supersede_clears_the_pointer(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        original = Memory(kind="event", text="исходный факт", source="user")
        session.add(original)
        await session.commit()
        await session.refresh(original)
        new = Memory(kind="event", text="объединённый факт", source="consolidate")
        session.add(new)
        await session.commit()
        await session.refresh(new)
        before = _row_state(original)
        original.superseded_by = new.id
        await session.commit()
        await session.refresh(original)
        after = _row_state(original)
        original_id = original.id

    run_id = await _reversible_done_run(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add(
            IdleChange(
                run_id=run_id, table_name="memory", row_id=original_id,
                op="supersede", before=before, after=after,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    async with sessionmaker() as session:
        original = await session.get(Memory, original_id)
        assert original.superseded_by is None


async def test_undo_writes_a_state_change_row_with_source_undo(sessionmaker):
    clock = _clock()
    run_id = await _reversible_done_run(sessionmaker, clock)

    async with sessionmaker() as session:
        await undo_run(session, Settings(), run_id, clock=clock)

    async with sessionmaker() as session:
        rows = (await session.execute(select(StateChange))).scalars().all()
    assert any(row.source == "undo" for row in rows)


async def test_partial_conflict_is_reported_and_that_row_is_left_alone(sessionmaker):
    """A row changed by something else since the idle run: undo skips it
    and reports the conflict, but the rest of the run still restores."""
    clock = _clock()
    async with sessionmaker() as session:
        untouched = Memory(kind="event", text="факт A", source="consolidate")
        conflicted = Memory(kind="event", text="факт B", source="consolidate")
        session.add_all([untouched, conflicted])
        await session.commit()
        await session.refresh(untouched)
        await session.refresh(conflicted)
        after_untouched = _row_state(untouched)
        after_conflicted = _row_state(conflicted)
        untouched_id, conflicted_id = untouched.id, conflicted.id

        # Something else edits `conflicted` after the idle run's `after`
        # snapshot was taken.
        conflicted.text = "изменено кем-то ещё"
        await session.commit()

    run_id = await _reversible_done_run(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add_all(
            [
                IdleChange(
                    run_id=run_id, table_name="memory", row_id=untouched_id,
                    op="insert", after=after_untouched,
                ),
                IdleChange(
                    run_id=run_id, table_name="memory", row_id=conflicted_id,
                    op="insert", after=after_conflicted,
                ),
            ]
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    assert result.restored == 1
    assert result.skipped_conflicts == 1
    async with sessionmaker() as session:
        assert await session.get(Memory, untouched_id) is None  # restored (deleted)
        still_there = await session.get(Memory, conflicted_id)
        assert still_there is not None
        assert still_there.text == "изменено кем-то ещё"


async def test_refuses_non_reversible(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        run = IdleRun(kind="backfill", local_date=clock.now_utc().date(), status="done", reversible=False)
        session.add(run)
        await session.commit()
        await session.refresh(run)

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run.id, clock=clock)
    assert result.status == "refused"
    assert result.reason == REFUSAL_NOT_REVERSIBLE


async def test_refuses_not_done(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        run = IdleRun(kind="backfill", local_date=clock.now_utc().date(), status="running", reversible=True)
        session.add(run)
        await session.commit()
        await session.refresh(run)

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run.id, clock=clock)
    assert result.status == "refused"
    assert result.reason == REFUSAL_NOT_DONE


async def test_refuses_already_undone(sessionmaker):
    clock = _clock()
    async with sessionmaker() as session:
        run = IdleRun(
            kind="backfill", local_date=clock.now_utc().date(), status="undone",
            reversible=True, undone_at=clock.now_utc(),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run.id, clock=clock)
    assert result.status == "refused"
    assert result.reason == REFUSAL_ALREADY_UNDONE


async def test_refuses_older_than_idle_undo_days(sessionmaker):
    base_clock = _clock()
    async with sessionmaker() as session:
        run = IdleRun(kind="backfill", local_date=base_clock.now_utc().date(), status="done", reversible=True)
        session.add(run)
        await session.commit()
        await session.refresh(run)

    later_clock = _clock(days=8)
    async with sessionmaker() as session:
        result = await undo_run(session, Settings(IDLE_UNDO_DAYS=7), run.id, clock=later_clock)
    assert result.status == "refused"
    assert result.reason == REFUSAL_TOO_OLD


async def test_atomicity_a_missing_row_is_a_conflict_not_a_crash_and_the_rest_still_commits(
    sessionmaker,
):
    """A row an idle_change points at has since been hard-deleted by
    something else -- a conflict for that one change, reported and
    skipped, while the rest of the same run's changes still apply and
    the whole transaction commits (`run.status` ends `undone`, not left
    half-applied)."""
    clock = _clock()
    async with sessionmaker() as session:
        gone = Memory(kind="event", text="будет удалён", source="consolidate")
        kept = Memory(kind="event", text="факт", source="consolidate")
        session.add_all([gone, kept])
        await session.commit()
        await session.refresh(gone)
        await session.refresh(kept)
        gone_after = _row_state(gone)
        kept_after = _row_state(kept)
        gone_id, kept_id = gone.id, kept.id
        # Hard-deleted by something else after the idle run -- the
        # change row still points at row_id=gone_id, but the row is gone.
        await session.delete(gone)
        await session.commit()

    run_id = await _reversible_done_run(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add_all(
            [
                IdleChange(
                    run_id=run_id, table_name="memory", row_id=gone_id,
                    op="insert", after=gone_after,
                ),
                IdleChange(
                    run_id=run_id, table_name="memory", row_id=kept_id,
                    op="insert", after=kept_after,
                ),
            ]
        )
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    assert result.restored == 1
    assert result.skipped_conflicts == 1
    async with sessionmaker() as session:
        run = await session.get(IdleRun, run_id)
        assert run.status == "undone", "the transaction commits fully despite the one conflict"
        assert await session.get(Memory, kept_id) is None, "the successful change still applied"


async def _consolidate_shaped_run(sessionmaker, clock):
    """What 6b's consolidate will log: one merged insert, then each
    original superseded by it. Returns (run_id, merged_id, originals'
    full `before` rows keyed by id)."""
    async with sessionmaker() as session:
        a = Memory(kind="preference", text="любит чай", source="extractor")
        b = Memory(kind="preference", text="пьёт чай по утрам", source="extractor")
        session.add_all([a, b])
        await session.commit()
        await session.refresh(a)
        await session.refresh(b)
        before = {a.id: _row_state(a), b.id: _row_state(b)}

        merged = Memory(kind="preference", text="любит чай, пьёт по утрам", source="consolidate")
        session.add(merged)
        await session.commit()
        await session.refresh(merged)
        merged_after = _row_state(merged)
        a.superseded_by = merged.id
        b.superseded_by = merged.id
        await session.commit()
        await session.refresh(a)
        await session.refresh(b)
        after = {a.id: _row_state(a), b.id: _row_state(b)}
        merged_id = merged.id

    run_id = await _reversible_done_run(sessionmaker, clock)
    async with sessionmaker() as session:
        session.add(
            IdleChange(run_id=run_id, table_name="memory", row_id=merged_id, op="insert",
                       after=merged_after)
        )
        for row_id in before:
            session.add(
                IdleChange(run_id=run_id, table_name="memory", row_id=row_id, op="supersede",
                           before=before[row_id], after=after[row_id])
            )
        await session.commit()
    return run_id, merged_id, before


async def test_consolidate_shaped_undo_restores_the_originals_exactly(sessionmaker):
    clock = _clock()
    run_id, merged_id, before = await _consolidate_shaped_run(sessionmaker, clock)

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert (result.status, result.restored, result.skipped_conflicts) == ("ok", 3, 0)
    async with sessionmaker() as session:
        assert await session.get(Memory, merged_id) is None
        for row_id, state in before.items():
            assert _row_state(await session.get(Memory, row_id)) == state


async def test_live_retrieval_of_the_merged_memory_is_not_a_conflict(sessionmaker):
    """Chat bumping last_used_at/use_count is use, not an edit."""
    clock = _clock()
    run_id, merged_id, _before = await _consolidate_shaped_run(sessionmaker, clock)
    async with sessionmaker() as session:
        merged = await session.get(Memory, merged_id)
        merged.use_count = merged.use_count + 1
        merged.last_used_at = clock.now_utc()
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.skipped_conflicts == 0
    async with sessionmaker() as session:
        assert await session.get(Memory, merged_id) is None


async def test_a_conflicted_original_keeps_the_merged_row_it_points_at(sessionmaker):
    """If an original was edited since, its supersede is skipped -- and
    the merged row it still points at must stay too, not break the FK."""
    clock = _clock()
    run_id, merged_id, before = await _consolidate_shaped_run(sessionmaker, clock)
    edited_id = min(before)
    async with sessionmaker() as session:
        edited = await session.get(Memory, edited_id)
        edited.pinned = True
        await session.commit()

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert result.status == "ok"
    assert result.restored == 1
    assert result.skipped_conflicts == 2
    async with sessionmaker() as session:
        assert await session.get(Memory, merged_id) is not None
        assert (await session.get(Memory, edited_id)).superseded_by == merged_id
        other = await session.get(Memory, max(before))
        assert _row_state(other) == before[max(before)]
