"""create phase1 tables

Revision ID: aed45828669b
Revises: e23dfc90818e
Create Date: 2026-09-21 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'aed45828669b'
down_revision: Union[str, Sequence[str], None] = 'e23dfc90818e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'message',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('ooc', sa.Boolean(), nullable=False),
        sa.Column('update_id', sa.BigInteger(), nullable=True),
        sa.Column('reply_to_update', sa.BigInteger(), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('model', sa.String(), nullable=True),
        sa.Column('tokens_in', sa.Integer(), nullable=True),
        sa.Column('tokens_cached', sa.Integer(), nullable=True),
        sa.Column('tokens_out', sa.Integer(), nullable=True),
        sa.Column('usd_cost', sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['update_id'], ['telegram_update.update_id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('reply_to_update'),
    )

    op.create_table(
        'user_state',
        sa.Column('id', sa.Integer(), autoincrement=False, server_default=sa.text('1'), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('persona_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('intensity', sa.Integer(), server_default=sa.text('3'), nullable=False),
        sa.Column('timezone', sa.String(), server_default=sa.text("'Europe/Paris'"), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('id = 1', name='ck_user_state_id_singleton'),
        sa.CheckConstraint('intensity between 1 and 5', name='ck_user_state_intensity_range'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'state_change',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('field', sa.String(), nullable=True),
        sa.Column('old_value', sa.String(), nullable=True),
        sa.Column('new_value', sa.String(), nullable=True),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'persona_version',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('sha256', sa.String(), nullable=False),
        sa.Column('body', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sha256'),
    )

    op.create_table(
        'spend_ledger',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('category', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=True),
        sa.Column('tokens_in', sa.Integer(), nullable=True),
        sa.Column('tokens_cached', sa.Integer(), nullable=True),
        sa.Column('tokens_out', sa.Integer(), nullable=True),
        sa.Column('usd_cost', sa.Numeric(precision=10, scale=6), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_spend_ledger_local_date', 'spend_ledger', ['local_date'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_spend_ledger_local_date', table_name='spend_ledger')
    op.drop_table('spend_ledger')
    op.drop_table('persona_version')
    op.drop_table('state_change')
    op.drop_table('user_state')
    op.drop_table('message')
