"""board_sync_run: allow the `busy` outcome.

A board edited less than BOARD_SYNC_QUIET_S ago is not written to; the
pass records it as `busy` (its planner side was still brought up to
date).

Revision ID: b5d9e1f3a7c2
Revises: e4b7d2a9c318
"""

from __future__ import annotations

from alembic import op

revision = "b5d9e1f3a7c2"
down_revision = "e4b7d2a9c318"
branch_labels = None
depends_on = None

_OLD = (
    "outcome in ('ok', 'changed', 'unavailable', 'refused', 'planner_unavailable',"
    " 'no_boards', 'missing', 'not_board')"
)
_NEW = (
    "outcome in ('ok', 'changed', 'busy', 'unavailable', 'refused', 'planner_unavailable',"
    " 'no_boards', 'missing', 'not_board')"
)


def upgrade() -> None:
    op.drop_constraint("ck_board_sync_run_outcome", "board_sync_run", type_="check")
    op.create_check_constraint("ck_board_sync_run_outcome", "board_sync_run", _NEW)


def downgrade() -> None:
    op.execute("delete from board_sync_run where outcome = 'busy'")
    op.drop_constraint("ck_board_sync_run_outcome", "board_sync_run", type_="check")
    op.create_check_constraint("ck_board_sync_run_outcome", "board_sync_run", _OLD)
