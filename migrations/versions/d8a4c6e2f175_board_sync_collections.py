"""board_pair.lane, board_sync_run.tasks_moved and collections_created

Board sync, one planner collection per board and columns by name
(docs/decisions.md, "Collection per board and columns by name"): a
board's tasks live in its own planner collection, made from its lanes,
and a card's lane is carried to the column of the same name.

- `board_pair.lane` is the name of the planner column the task sat in
  when both sides last agreed (the board's lane of that name, when it has
  one), null until a pass compared columns by name; a null row compares
  states by role only, as before, and is filled by the next pass. User
  content like `title` (a lane name is the user's words).
- `board_sync_run.tasks_moved` counts the paired tasks in no collection a
  pass moved into the board's, default 0; `collections_created` the
  collections it made, default 0.

Reversible: downgrade drops the three columns; the planner keeps the
collections and the moves the passes made, and the next pass after an
upgrade compares states by role once more before it fills `lane` again.

Revision ID: d8a4c6e2f175
Revises: b5d3f8a1c264
Create Date: 2026-11-11 18:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8a4c6e2f175'
down_revision: str | Sequence[str] | None = 'b5d3f8a1c264'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('board_pair', sa.Column('lane', sa.String(), nullable=True))
    op.add_column(
        'board_sync_run', sa.Column('tasks_moved', sa.Integer(), server_default=sa.text('0'), nullable=False)
    )
    op.add_column(
        'board_sync_run', sa.Column('collections_created', sa.Integer(), server_default=sa.text('0'), nullable=False)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('board_sync_run', 'collections_created')
    op.drop_column('board_sync_run', 'tasks_moved')
    op.drop_column('board_pair', 'lane')
