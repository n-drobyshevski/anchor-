"""board_pair, board_sync_run: the task boards kept in step with the planner

Board sync (docs/decisions.md, "Board sync"): the user's Kanban Starlane
boards at the vault's root (`Work tasks.md`, `General tasks.md`) and the
planner's tasks with the board's tag are kept in step both ways, by a
background job (app/planner/board_job.py) that merges three ways against
the last synced snapshot (app/planner/board_sync.py).

- `board_pair` is one tracked pair: the board's file name, the card's
  block id, the planner task's id, and the snapshot of the three synced
  fields (`title`, `done`, `due`) plus `status` (`ok`, or `held` while a
  field cannot be carried). Unique per board on the block id and on the
  task id. **It holds card text** (the title, from both sides): user
  content, so `/export` includes it and `/delete` truncates it.
- `board_sync_run` is one board's outcome in one pass (or, with `board`
  null, a pass that never reached a board): an outcome code and counts.
  `/boards` and the daily digest read it; the job deletes rows older
  than two weeks. `/export` and `/delete` cover it too: `board` is the
  user's own file name.

**No debug view** for either: a board's name is a file name the user
chose (vault_file's `path` is kept out of debug for the same reason), and
a pair's title is the user's own words. Debugging a pass uses Railway's
logs, which carry an event name and counts only.

Reversible: downgrade drops both tables; the boards and the planner keep
whatever the last pass wrote, and the next pass after an upgrade pairs
them again by title.

Revision ID: c7e2a9d4f136
Revises: d8a3f6c1b925
Create Date: 2026-11-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c7e2a9d4f136'
down_revision: Union[str, Sequence[str], None] = 'd8a3f6c1b925'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OUTCOMES = (
    "'ok', 'changed', 'unavailable', 'refused', 'planner_unavailable', 'no_boards', 'missing', 'not_board'"
)
COUNTS = (
    'cards_added',
    'cards_changed',
    'cards_tagged',
    'cards_removed',
    'tasks_created',
    'tasks_changed',
    'tasks_untagged',
    'paired',
    'conflicts',
    'skipped',
    'waiting',
    'held',
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'board_pair',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('board', sa.String(), nullable=False),
        sa.Column('block_id', sa.String(), nullable=False),
        sa.Column('task_id', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('done', sa.Boolean(), nullable=False),
        sa.Column('due', sa.Date(), nullable=True),
        sa.Column('status', sa.String(), server_default=sa.text("'ok'"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("block_id ~ '^[A-Za-z0-9-]{1,64}$'", name='ck_board_pair_block_id'),
        sa.CheckConstraint("status in ('ok', 'held')", name='ck_board_pair_status'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('board', 'block_id', name='uq_board_pair_block'),
        sa.UniqueConstraint('board', 'task_id', name='uq_board_pair_task'),
    )
    op.create_table(
        'board_sync_run',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('board', sa.String(), nullable=True),
        sa.Column('outcome', sa.String(), nullable=False),
        *(sa.Column(name, sa.Integer(), server_default=sa.text('0'), nullable=False) for name in COUNTS),
        sa.CheckConstraint(f'outcome in ({OUTCOMES})', name='ck_board_sync_run_outcome'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_board_sync_run_at', 'board_sync_run', ['at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_board_sync_run_at', table_name='board_sync_run')
    op.drop_table('board_sync_run')
    op.drop_table('board_pair')
