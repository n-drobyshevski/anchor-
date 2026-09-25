"""The undo engine (Phase 6 plan section 6.1's `undo.py`; §7's undo
semantics; approved plan §5).

`undo_run` replays one idle_run's `idle_change` rows in reverse `id`
order, in the caller's transaction: `insert` -> delete the row,
`supersede` -> clear the pointer, `close` -> reactivate, `update` ->
restore `before`. Every row is re-checked against its logged `after`
before being touched -- a mismatch means something else changed it
since the idle run, and that one row is skipped and reported rather
than silently overwritten (plan section 7: "если запись изменили после
idle-прогона, отмена для неё пропускается и это отражается в
ответе": «часть изменений уже перезаписана»).

Refuses (never raises) a run that is not reversible, not `done`, already
undone, or older than `IDLE_UNDO_DAYS`. 6a tests this against synthetic
`idle_change` rows on `memory` and `notebook_entry`; the real writers
(consolidate, reflect) arrive in 6b.

**A merge group is one conflict unit (W3 finding).** A consolidate
merge's `supersede` changes each compare clean against their own
row's state even after the user edits or pins the *merged* row they
fed into -- neither of those touches the original rows at all. Without
`_conflicted_merge_targets`'s pre-scan, undo would restore the
originals to active right next to the user's post-merge change,
duplicating the fact the merge was meant to consolidate.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.core.idle.rowstate import row_state as _row_state
from app.db.models import IdleChange, IdleRun, Memory, NotebookEntry, StateChange

logger = logging.getLogger(__name__)

_TABLES = {"memory": Memory, "notebook_entry": NotebookEntry}

# Usage counters that live chat bumps on every retrieval. They are not
# edits: a merged memory that was merely *used* since the idle run is
# still exactly what the run wrote, so these are left out of the
# conflict comparison and are never rewound by a restore either.
_VOLATILE_COLUMNS = {"memory": frozenset({"last_used_at", "use_count"})}

REFUSAL_NOT_FOUND = "not_found"
REFUSAL_NOT_REVERSIBLE = "not_reversible"
REFUSAL_NOT_DONE = "not_done"
REFUSAL_ALREADY_UNDONE = "already_undone"
REFUSAL_TOO_OLD = "too_old"

STATUS_OK = "ok"
STATUS_REFUSED = "refused"


@dataclasses.dataclass(frozen=True)
class UndoResult:
    """`status` is `"ok"` or `"refused"`; `reason` is set only on a
    refusal (one of the REFUSAL_* constants above)."""

    status: str
    restored: int = 0
    skipped_conflicts: int = 0
    reason: str | None = None


def _comparable(table: str, state: dict | None) -> dict | None:
    if state is None:
        return None
    volatile = _VOLATILE_COLUMNS.get(table, frozenset())
    return {key: value for key, value in state.items() if key not in volatile}


def _decode(model, key: str, value):
    """Reverse `_encode` for one column, using the model's own column
    type as the source of truth for what it should become."""
    if value is None:
        return None
    column = model.__table__.columns[key]
    try:
        py_type = column.type.python_type
    except NotImplementedError:  # pragma: no cover - no such column here
        return value
    if py_type is decimal.Decimal and not isinstance(value, decimal.Decimal):
        return decimal.Decimal(str(value))
    if py_type is datetime.datetime and isinstance(value, str):
        return datetime.datetime.fromisoformat(value)
    if py_type is datetime.date and isinstance(value, str):
        return datetime.date.fromisoformat(value)
    return value


async def _apply_one(session: AsyncSession, change: IdleChange) -> bool:
    """Reverse one idle_change row. Returns True on success, False on a
    conflict (current state != logged `after`) or an unknown shape."""
    model = _TABLES.get(change.table_name)
    if model is None:
        return False

    row = await session.get(model, change.row_id, with_for_update=True)
    current = _row_state(row) if row is not None else None
    if change.after is not None and _comparable(change.table_name, current) != _comparable(
        change.table_name, change.after
    ):
        return False

    if change.op == "insert":
        if row is None:
            return True
        # Something written since may point at the inserted memory (a
        # later supersede, or an earlier change of this run that was
        # itself skipped as a conflict). Deleting it would break that
        # pointer, so it counts as overwritten and stays.
        if model is Memory:
            referenced = await session.execute(
                select(Memory.id).where(Memory.superseded_by == row.id).limit(1)
            )
            if referenced.first() is not None:
                return False
        await session.delete(row)
        await session.flush()
        return True

    if change.op in ("supersede", "close", "update"):
        if row is None or change.before is None:
            return False
        for key, value in change.before.items():
            if key in ("id", "created_at") or key in _VOLATILE_COLUMNS.get(
                change.table_name, ()
            ):
                continue
            setattr(row, key, _decode(model, key, value))
        return True

    return False


async def _conflicted_merge_targets(session: AsyncSession, changes: list[IdleChange]) -> set[int]:
    """Which `insert`-op memory ids in `changes` can no longer be safely
    un-inserted -- gone, or their current state has drifted from what
    the run wrote (W3 finding).

    A consolidate merge logs one `insert` (the merged row) plus one
    `supersede` per original it replaced, all pointing `superseded_by`
    at that same insert. `_apply_one` processes changes newest-first, so
    it reaches those `supersede` rows *before* the `insert` -- and a
    `supersede` change's own before/after never mentions the merged row
    beyond its id, only the original's own fields, so a later edit or
    pin of the *merged* row (which touches only the merged row's own
    state) does not, by itself, make any `supersede` change look
    conflicted. Left alone, undo would restore the originals to active
    right next to the user's post-merge edit or pin -- exactly the
    duplicate-facts case this pre-scan exists to catch, applying plan
    section 7's "часть изменений уже перезаписана" to the whole merge
    group rather than row by row.

    Deliberately narrower than `_apply_one`'s own insert-branch check:
    it does not also treat "another row's superseded_by still points at
    it" as a conflict here, because at pre-scan time that is true of
    *every* live, not-yet-undone consolidate merge (the originals still
    point at it) -- that third check only means something once this
    group's own `supersede` changes have actually been applied or
    skipped, which is `_apply_one`'s own job during the real pass below,
    not this one.
    """
    conflicted: set[int] = set()
    for change in changes:
        if change.table_name != "memory" or change.op != "insert" or change.after is None:
            continue
        row = await session.get(Memory, change.row_id)
        current = _row_state(row) if row is not None else None
        if _comparable("memory", current) != _comparable("memory", change.after):
            conflicted.add(change.row_id)
    return conflicted


async def undo_run(
    session: AsyncSession, settings: Settings, run_id: int, *, clock: Clock
) -> UndoResult:
    """Undo `run_id`'s writes, or refuse. One transaction; the caller commits."""
    run = await session.get(IdleRun, run_id, with_for_update=True)
    if run is None:
        return UndoResult(status=STATUS_REFUSED, reason=REFUSAL_NOT_FOUND)
    if not run.reversible:
        return UndoResult(status=STATUS_REFUSED, reason=REFUSAL_NOT_REVERSIBLE)
    # Checked before the plain "not done" refusal: `status='undone'` is
    # itself `!= 'done'`, so without this ordering an already-undone run
    # would always report the less specific NOT_DONE instead.
    if run.undone_at is not None:
        return UndoResult(status=STATUS_REFUSED, reason=REFUSAL_ALREADY_UNDONE)
    if run.status != "done":
        return UndoResult(status=STATUS_REFUSED, reason=REFUSAL_NOT_DONE)
    if clock.now_utc() - run.created_at >= datetime.timedelta(days=settings.IDLE_UNDO_DAYS):
        return UndoResult(status=STATUS_REFUSED, reason=REFUSAL_TOO_OLD)

    result = await session.execute(
        select(IdleChange).where(IdleChange.run_id == run_id).order_by(IdleChange.id.desc())
    )
    changes = list(result.scalars().all())
    conflicted_merge_targets = await _conflicted_merge_targets(session, changes)

    restored = 0
    conflicts = 0
    for change in changes:
        # A supersede feeding a merged/edited row whose own insert is
        # already known-conflicted (see _conflicted_merge_targets) is
        # skipped here too, before `_apply_one` ever gets to it -- that
        # function's own per-row check has no way to see this, since
        # this row's own before/after never changed.
        if (
            change.table_name == "memory"
            and change.op == "supersede"
            and change.after is not None
            and change.after.get("superseded_by") in conflicted_merge_targets
        ):
            conflicts += 1
            continue
        if await _apply_one(session, change):
            restored += 1
        else:
            conflicts += 1

    run.status = "undone"
    run.undone_at = clock.now_utc()
    # A bare StateChange insert, not app.core.state.record_change():
    # app/core/idle/ may never import app.core.state (it writes
    # user_state, which idle must never touch -- see
    # tests/test_idle_isolation.py), but state_change itself is an
    # audit table, not user_state, and the plan requires this row
    # ("writes state_change source 'undo'"). Same values that helper
    # would have written -- field/old_value/new_value carry an id and a
    # word, never content, matching app/core/state.py's own rule.
    session.add(
        StateChange(
            field="idle_run", old_value=str(run_id), new_value="undone", source="undo"
        )
    )
    await session.commit()
    logger.info(
        "idle run undone",
        extra={"run_id": run_id, "restored": restored, "conflicts": conflicts},
    )
    return UndoResult(status=STATUS_OK, restored=restored, skipped_conflicts=conflicts)


__all__ = [
    "REFUSAL_ALREADY_UNDONE",
    "REFUSAL_NOT_DONE",
    "REFUSAL_NOT_FOUND",
    "REFUSAL_NOT_REVERSIBLE",
    "REFUSAL_TOO_OLD",
    "STATUS_OK",
    "STATUS_REFUSED",
    "UndoResult",
    "undo_run",
]
