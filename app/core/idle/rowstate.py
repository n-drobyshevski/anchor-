"""`row_state` -- the one JSON-safe row encoder idle writes use for
`idle_change.before`/`after` (Phase 6 plan section 3; milestone 6b).

Extracted from `app/core/idle/undo.py`'s own private `_row_state` (6a),
which used exactly this shape already. 6b needs the same encoding from
the *writing* side too -- `consolidate.py` and `notebook.apply_plan`'s
`on_change` hook both snapshot a row before and after mutating it, and
those snapshots must be byte-identical to what `undo.py` computes when
it later re-reads the row to check for a conflict. Two independent
implementations of "encode a row" agreeing today and drifting after the
next column type change is exactly the kind of bug this module exists
to rule out -- so there is exactly one implementation, imported by both
sides.

`app.core.export.encode` is reused rather than re-derived for the same
reason `undo.py`'s own docstring gives: a value that round-trips
through `/export` round-trips through idle's before/after snapshots
identically.
"""

from __future__ import annotations

from app.core.export import encode


def row_state(row) -> dict:
    """Every column of `row`, JSON-safe. `row` must be a mapped instance
    (its `__table__.columns` gives the column list)."""
    return {column.name: encode(getattr(row, column.name)) for column in row.__table__.columns}


__all__ = ["row_state"]
