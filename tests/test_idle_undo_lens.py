"""L5: undo across the migration that gave `notebook_entry` its lens
columns (the L5 spec section 2, "undo.py"; migration 3d3efa0cbc9a).

`idle_change` snapshots taken before the migration lack
`lens_round_id` and `lens_note_ids`, while the row read today has both.
The conflict check compares only the columns the logged `after` has, so
undoing a pre-deploy reflect run within `IDLE_UNDO_DAYS` restores its
changes instead of reporting each one as a conflict. Snapshots taken
after the migration carry both columns, and undo restores them.

Synthetic rows only.
"""

from __future__ import annotations

import datetime

import pytest

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.idle.rowstate import row_state
from app.core.idle.undo import undo_run
from app.db.models import IdleChange, IdleRun, LensRound, NotebookEntry

pytestmark = pytest.mark.asyncio

LENS_COLUMNS = ("lens_round_id", "lens_note_ids")


def _clock() -> FrozenClock:
    return FrozenClock(datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc))


def _pre_l5(state: dict) -> dict:
    """A snapshot as a run before the migration logged it."""
    return {key: value for key, value in state.items() if key not in LENS_COLUMNS}


async def _run(sessionmaker, clock) -> int:
    async with sessionmaker() as session:
        run = IdleRun(kind="reflect", local_date=clock.now_utc().date(), status="done", reversible=True)
        session.add(run)
        await session.commit()
        return run.id


async def _log(sessionmaker, run_id: int, *changes: tuple[int, str, dict | None, dict | None]) -> None:
    async with sessionmaker() as session:
        for row_id, op, before, after in changes:
            session.add(
                IdleChange(
                    run_id=run_id, table_name="notebook_entry", row_id=row_id, op=op,
                    before=before, after=after,
                )
            )
        await session.commit()


async def _entry(sessionmaker, text: str, **columns) -> NotebookEntry:
    async with sessionmaker() as session:
        entry = NotebookEntry(kind="open_thread", text=text, source="anchor", **columns)
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        return entry


async def _update(sessionmaker, entry_id: int, **values) -> tuple[dict, dict]:
    """Change the row as a run would; its before and after states."""
    async with sessionmaker() as session:
        entry = await session.get(NotebookEntry, entry_id)
        before = row_state(entry)
        for key, value in values.items():
            setattr(entry, key, value)
        await session.commit()
        await session.refresh(entry)
        return before, row_state(entry)


async def test_a_run_logged_before_the_migration_undoes_without_conflicts(sessionmaker):
    """An insert, an update and a close, each logged without the lens
    columns: all three restore, and the lens columns stay NULL/'{}'."""
    clock = _clock()
    inserted = await _entry(sessionmaker, "новая тема")
    updated = await _entry(sessionmaker, "старый текст")
    closed = await _entry(sessionmaker, "закрытая тема")
    insert_after = row_state(inserted)
    update_before, update_after = await _update(sessionmaker, updated.id, text="новый текст")
    close_before, close_after = await _update(
        sessionmaker, closed.id, active=False, closed_by="anchor", closed_at=clock.now_utc()
    )
    for state in (insert_after, update_after, close_after):
        assert set(LENS_COLUMNS) <= state.keys()

    run_id = await _run(sessionmaker, clock)
    await _log(
        sessionmaker,
        run_id,
        (inserted.id, "insert", None, _pre_l5(insert_after)),
        (updated.id, "update", _pre_l5(update_before), _pre_l5(update_after)),
        (closed.id, "close", _pre_l5(close_before), _pre_l5(close_after)),
    )

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert (result.status, result.restored, result.skipped_conflicts) == ("ok", 3, 0)
    async with sessionmaker() as session:
        assert await session.get(NotebookEntry, inserted.id) is None
        entry = await session.get(NotebookEntry, updated.id)
        assert (entry.text, entry.lens_round_id, entry.lens_note_ids) == ("старый текст", None, [])
        entry = await session.get(NotebookEntry, closed.id)
        assert (entry.active, entry.closed_by, entry.lens_round_id, entry.lens_note_ids) == (
            True, None, None, []
        )


async def test_a_pre_migration_snapshot_still_detects_an_edit_since(sessionmaker):
    """Narrowing the check to the logged columns keeps every one of them:
    a text edited after the run is still a conflict, and left alone."""
    clock = _clock()
    entry = await _entry(sessionmaker, "старый текст")
    before, after = await _update(sessionmaker, entry.id, text="текст прогона")
    await _update(sessionmaker, entry.id, text="правка пользователя")

    run_id = await _run(sessionmaker, clock)
    await _log(sessionmaker, run_id, (entry.id, "update", _pre_l5(before), _pre_l5(after)))

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert (result.status, result.restored, result.skipped_conflicts) == ("ok", 0, 1)
    async with sessionmaker() as session:
        assert (await session.get(NotebookEntry, entry.id)).text == "правка пользователя"


async def _reflect_round(sessionmaker) -> int:
    async with sessionmaker() as session:
        row = LensRound(consumer="reflect", outcome="grounded", selected_note_ids=[4, 5])
        session.add(row)
        await session.commit()
        return row.id


async def test_undo_restores_the_lens_columns_a_grounded_update_wrote(sessionmaker):
    clock = _clock()
    round_id = await _reflect_round(sessionmaker)
    entry = await _entry(sessionmaker, "старый текст")
    before, after = await _update(
        sessionmaker, entry.id, text="текст на линзе", lens_round_id=round_id, lens_note_ids=[5, 4]
    )
    assert (after["lens_round_id"], after["lens_note_ids"]) == (round_id, [5, 4])

    run_id = await _run(sessionmaker, clock)
    await _log(sessionmaker, run_id, (entry.id, "update", before, after))

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert (result.restored, result.skipped_conflicts) == (1, 0)
    async with sessionmaker() as session:
        restored = await session.get(NotebookEntry, entry.id)
        assert (restored.text, restored.lens_round_id, restored.lens_note_ids) == ("старый текст", None, [])


async def test_undo_puts_back_the_lens_columns_an_ungrounded_update_cleared(sessionmaker):
    """The per-scene notebook's update clears an entry's lens columns
    (the L5 spec section 3); undoing it brings the grounding back."""
    clock = _clock()
    round_id = await _reflect_round(sessionmaker)
    entry = await _entry(sessionmaker, "текст на линзе", lens_round_id=round_id, lens_note_ids=[4])
    before, after = await _update(
        sessionmaker, entry.id, text="текст сцены", lens_round_id=None, lens_note_ids=[]
    )

    run_id = await _run(sessionmaker, clock)
    await _log(sessionmaker, run_id, (entry.id, "update", before, after))

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert (result.restored, result.skipped_conflicts) == (1, 0)
    async with sessionmaker() as session:
        restored = await session.get(NotebookEntry, entry.id)
        assert (restored.text, restored.lens_round_id, restored.lens_note_ids) == (
            "текст на линзе", round_id, [4]
        )


async def test_a_lens_column_changed_since_a_post_migration_snapshot_is_a_conflict(sessionmaker):
    clock = _clock()
    round_id = await _reflect_round(sessionmaker)
    entry = await _entry(sessionmaker, "старый текст")
    before, after = await _update(
        sessionmaker, entry.id, text="текст на линзе", lens_round_id=round_id, lens_note_ids=[4]
    )
    await _update(sessionmaker, entry.id, lens_note_ids=[4, 5])

    run_id = await _run(sessionmaker, clock)
    await _log(sessionmaker, run_id, (entry.id, "update", before, after))

    async with sessionmaker() as session:
        result = await undo_run(session, Settings(), run_id, clock=clock)

    assert (result.restored, result.skipped_conflicts) == (0, 1)
    async with sessionmaker() as session:
        assert (await session.get(NotebookEntry, entry.id)).lens_note_ids == [4, 5]
