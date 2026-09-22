"""create checkin, add user_state streak/awaiting columns

Milestone 2d (phase-2 plan sections 4 and 9). Completes the phase-2
user_state shape: 2c added focus/due with the proposal-accept path,
this adds the streak and the pending-step fields.

`checkin.tg_message_id` is not in the plan's SQL. Section 9's stale-
button rule needs it: the callback data it specifies carries no
check-in id, so "is this button from the current check-in?" can only be
answered by message id. `proposal` carries the same column for the same
reason.

Reversible. Downgrade destroys check-in history and the streak, which
are not derivable from anything else -- say so rather than inherit 2c's
gentler note.

Revision ID: e5b71c93d820
Revises: d4a83bc1f59e
Create Date: 2026-09-22 12:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e5b71c93d820'
down_revision: Union[str, Sequence[str], None] = 'd4a83bc1f59e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'checkin',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('day_rating', sa.Integer(), nullable=True),
        sa.Column('due_result', sa.String(), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('tg_message_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('day_rating between 1 and 5', name='ck_checkin_day_rating'),
        sa.CheckConstraint(
            "due_result in ('done', 'partial', 'no', 'none')", name='ck_checkin_due_result'
        ),
        sa.CheckConstraint('char_length("note") <= 500', name='ck_checkin_note_length'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('local_date'),
    )

    op.add_column(
        'user_state',
        sa.Column('streak', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )
    op.add_column(
        'user_state', sa.Column('last_checkin_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column('user_state', sa.Column('awaiting', sa.String(), nullable=True))
    op.add_column('user_state', sa.Column('awaiting_ref', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema. Destroys check-in history and the streak."""
    op.drop_column('user_state', 'awaiting_ref')
    op.drop_column('user_state', 'awaiting')
    op.drop_column('user_state', 'last_checkin_at')
    op.drop_column('user_state', 'streak')
    op.drop_table('checkin')
