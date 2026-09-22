"""create safety_event

Hardening milestone H2. One row per safety-model call outcome, so that a
welfare classifier which has quietly stopped working looks different
from one with nothing to report.

Why a table rather than a column on spend_ledger: app/core/turn.py's
_ledger_only() returns early when the response is None, so a classifier
that timed out writes no ledger row at all -- the very outcome worth
recording is the one that table cannot hold. And a timeout, an error and
a fallback_hit cost nothing, so recording them as zero-cost ledger rows
would pollute today_by_category() and the daily-cap query, which is row
5 of the outbound gate.

Both columns are constrained, unlike spend_ledger.category. That one is
deliberately open because it records money already spent and a rejected
row would lose the record; here the vocabularies are closed sets of
constants in app/core/welfare.py, an unrecognized value is a bug, and
tests/test_safety_event.py pins the constants against these constraints.

Reversible, and cheap in both directions: the table holds no content and
nothing references it. A downgrade loses the observability history and
the /state welfare line goes back to reading zero -- no behaviour
depends on it.

Revision ID: a1c4e7b90d63
Revises: f3a91c7d02b5
Create Date: 2026-09-22 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1c4e7b90d63'
down_revision: Union[str, Sequence[str], None] = 'f3a91c7d02b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'safety_event',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            'ts',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('outcome', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=True),
        sa.CheckConstraint(
            "kind in ('welfare', 'extractor', 'tick')", name='ck_safety_event_kind'
        ),
        sa.CheckConstraint(
            "outcome in ('ok', 'parse_fail', 'timeout', 'error', 'fallback_hit')",
            name='ck_safety_event_outcome',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_safety_event_local_date', 'safety_event', ['local_date'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_safety_event_local_date', table_name='safety_event')
    op.drop_table('safety_event')
