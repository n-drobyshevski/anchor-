"""board_sub_pair, board_pair.deps, board_sync_run's subtask counts

Board sync, subtasks and dependencies (docs/decisions.md, "Board sync:
subtasks and dependencies"): a card's nested checklist is kept in step
with its planner task's subtasks, and its `after:` line with the task's
`blockedBy`.

- `board_sub_pair` is one tracked subtask pair: the board's file name and
  the subtask's block id (the key), the card it sits in, the subtask it
  sits under (null: right under the card), the planner subtask's id, the
  snapshot (`title`, `done`, `due`) and `status` (`ok`, `held`, or
  `detached`: the subtask left the card while the planner kept it, so the
  pair is let go of rather than the planner subtask deleted). **It holds
  subtask text**: user content, so `/export` includes it and `/delete`
  truncates it, and there is no debug view, like `board_pair`.
- `board_pair.deps` is the snapshot of a card's dependencies, the sorted
  planner task ids both sides last agreed on; null until compared once.
  `board_pair.deps_card` is null while the card's `after:` line shows
  `deps`; when the line could not be written, the task ids it resolved
  to as last read (its side of the merge, tracked apart).
- `board_sync_run` gains seven counts, default 0: `subs_added`,
  `subs_changed`, `subs_removed`, `subs_created`, `subs_detached`,
  `deps_changed`, `subs_paired`.

Reversible: downgrade drops the table and the columns; the boards and the
planner keep what the passes wrote, and the next pass after an upgrade
pairs subtasks again by title and starts the dependencies from the union
of both sides.

Revision ID: e4b7d2a9c318
Revises: c7e2a9d4f136
Create Date: 2026-11-10 22:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e4b7d2a9c318'
down_revision: str | Sequence[str] | None = 'c7e2a9d4f136'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COUNTS = (
    'subs_added',
    'subs_changed',
    'subs_removed',
    'subs_created',
    'subs_detached',
    'deps_changed',
    'subs_paired',
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'board_sub_pair',
        sa.Column('board', sa.String(), nullable=False),
        sa.Column('block_id', sa.String(), nullable=False),
        sa.Column('card_block_id', sa.String(), nullable=False),
        sa.Column('task_id', sa.String(), nullable=False),
        sa.Column('parent_block_id', sa.String(), nullable=True),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('done', sa.Boolean(), nullable=False),
        sa.Column('due', sa.Date(), nullable=True),
        sa.Column('status', sa.String(), server_default=sa.text("'ok'"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("block_id ~ '^[A-Za-z0-9-]{1,64}$'", name='ck_board_sub_pair_block_id'),
        sa.CheckConstraint("status in ('ok', 'held', 'detached')", name='ck_board_sub_pair_status'),
        sa.PrimaryKeyConstraint('board', 'block_id'),
        sa.UniqueConstraint('board', 'task_id', name='uq_board_sub_pair_task'),
    )
    op.add_column('board_pair', sa.Column('deps', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('board_pair', sa.Column('deps_card', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    for name in COUNTS:
        op.add_column('board_sync_run', sa.Column(name, sa.Integer(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    for name in reversed(COUNTS):
        op.drop_column('board_sync_run', name)
    op.drop_column('board_pair', 'deps_card')
    op.drop_column('board_pair', 'deps')
    op.drop_table('board_sub_pair')
