"""board_pair.state, board_sync_run.states_changed and states_held

Board sync, lanes by role (docs/decisions.md, "Board sync: lanes by
role"): a card's lane is read as its planner task's state (the done
lane: done, the first other lane: planned, any other: in progress).

- `board_pair.state` is the state both sides last agreed on (`planned`,
  `wip`, `done`), null until a pass compared states; a null row reads its
  base from `done` (done, else planned).
- `board_sync_run.states_changed` counts the pairs whose state a pass
  carried, default 0; `states_held` those the mass-change guard held back
  (part of `held`), default 0.

Reversible: downgrade drops the three columns; the boards and the planner keep
what the passes wrote, and the next pass after an upgrade starts the
states from `done` again.

Revision ID: b5d3f8a1c264
Revises: b5d9e1f3a7c2
Create Date: 2026-11-11 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b5d3f8a1c264'
down_revision: str | Sequence[str] | None = 'b5d9e1f3a7c2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('board_pair', sa.Column('state', sa.String(), nullable=True))
    op.create_check_constraint('ck_board_pair_state', 'board_pair', "state in ('planned', 'wip', 'done')")
    op.add_column(
        'board_sync_run', sa.Column('states_changed', sa.Integer(), server_default=sa.text('0'), nullable=False)
    )
    op.add_column(
        'board_sync_run', sa.Column('states_held', sa.Integer(), server_default=sa.text('0'), nullable=False)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('board_sync_run', 'states_held')
    op.drop_column('board_sync_run', 'states_changed')
    op.drop_constraint('ck_board_pair_state', 'board_pair', type_='check')
    op.drop_column('board_pair', 'state')
