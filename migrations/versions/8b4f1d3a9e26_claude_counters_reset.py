"""add vault_status.claude_counters_reset_at (reset Claude's write counters)

docs/decisions.md "Claude write counters can be reset": the user's
«сбросить счётчики» from `/claude limits` or the web app. Every hourly
and daily cap on Claude's writes counts only changesets started at or
after this moment; the ledger itself (undo needs it) is untouched. A
timestamp only -- no content. Reversible.

Revision ID: 8b4f1d3a9e26
Revises: 5d2e8a1f0c47
Create Date: 2026-09-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '8b4f1d3a9e26'
down_revision: Union[str, Sequence[str], None] = '5d2e8a1f0c47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'vault_status',
        sa.Column('claude_counters_reset_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('vault_status', 'claude_counters_reset_at')
