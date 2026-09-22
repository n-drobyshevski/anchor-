"""create journal and proposal, add user_state focus/due columns

Milestone 2c (phase-2 plan sections 4 and 8).

The four user_state columns are listed under milestone 2d in plan
section 15, but section 8's proposal-accept path writes them and that
path ships here. They arrive with the button as their only writer; 2d
adds /due and /focus alongside, plus the streak/check-in columns this
milestone has no use for.

Reversible. Downgrade destroys journal entries and undecided proposals,
both of which are derived from conversation the `message` table still
holds -- unlike 2b's memories, which were not derivable from anything.

Revision ID: d4a83bc1f59e
Revises: c92f5ad1e4b7
Create Date: 2026-09-22 11:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4a83bc1f59e'
down_revision: Union[str, Sequence[str], None] = 'c92f5ad1e4b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'journal',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('char_length("text") <= 240', name='ck_journal_text_length'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_journal_local_date', 'journal', ['local_date'])

    op.create_table(
        'proposal',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('field', sa.String(), nullable=False),
        sa.Column('value', sa.String(), nullable=False),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('status', sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('tg_message_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("field in ('due_action', 'focus_on', 'rule')", name='ck_proposal_field'),
        sa.CheckConstraint(
            "status in ('pending', 'accepted', 'rejected', 'expired')", name='ck_proposal_status'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_proposal_status', 'proposal', ['status'])

    op.add_column(
        'user_state',
        sa.Column('focus_on', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.add_column('user_state', sa.Column('focus_since', sa.DateTime(timezone=True), nullable=True))
    op.add_column('user_state', sa.Column('due_action', sa.String(), nullable=True))
    op.add_column('user_state', sa.Column('due_set_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user_state', 'due_set_at')
    op.drop_column('user_state', 'due_action')
    op.drop_column('user_state', 'focus_since')
    op.drop_column('user_state', 'focus_on')

    op.drop_index('ix_proposal_status', table_name='proposal')
    op.drop_table('proposal')
    op.drop_index('ix_journal_local_date', table_name='journal')
    op.drop_table('journal')
