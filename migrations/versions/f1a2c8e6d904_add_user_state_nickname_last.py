"""add user_state.nickname_last

Phase 5 milestone 5a (plan section 2 and section 15's file-by-file
list): the one piece of nickname state that survives between turns.
app/core/voice.py's choose_nickname() reads it to never repeat the same
nickname twice in a row, and remember_nickname() is its only writer -- a
targeted `UPDATE user_state SET nickname_last`, never update_state(), so
a nickname rotation leaves no state_change audit row (the same
reasoning 3a's set_counters() already applies to the traffic counters).

Nullable, with no default: NULL means "no nickname used yet", which is
exactly the singleton row's state the moment this column exists, and
the state it returns to after a `/delete`.

Revision ID: f1a2c8e6d904
Revises: d1b83f6c204e
Create Date: 2026-09-22 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f1a2c8e6d904'
down_revision: Union[str, Sequence[str], None] = 'd1b83f6c204e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('user_state', sa.Column('nickname_last', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user_state', 'nickname_last')
