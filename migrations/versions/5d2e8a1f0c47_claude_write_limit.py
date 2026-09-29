"""add claude_write_limit (user-set write caps)

docs/decisions.md "Claude write caps become settings": the rate/volume
caps on Claude's knowledge writes are tuned by hand from Telegram
(`/claude limits`) or the web app. One row per overridden cap, keyed by
its name; a missing row means the default in
app/core/claude_write_limits.py. A name and a number only -- no path,
no text. `vault_status.limits_push_pending` marks a change vaultd's
own copy has not taken yet (the vault sync pass retries the push).
Reversible.

Revision ID: 5d2e8a1f0c47
Revises: 027b3c0b323d
Create Date: 2026-09-29 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '5d2e8a1f0c47'
down_revision: Union[str, Sequence[str], None] = '027b3c0b323d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'claude_write_limit',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('value', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('value >= 0', name='ck_claude_write_limit_value'),
        sa.PrimaryKeyConstraint('name'),
    )
    op.add_column(
        'vault_status',
        sa.Column('limits_push_pending', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('vault_status', 'limits_push_pending')
    op.drop_table('claude_write_limit')
