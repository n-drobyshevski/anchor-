"""add spend_ledger.cost_source

Hardening milestone H4. Records which of two different kinds of number a
ledger row is carrying: 'vendor' when OpenRouter reported the cost
itself, 'computed' when it is our arithmetic over token counts at prices
from config.

compute_cost() has always preferred the vendor figure -- that part of H4
was already built -- but nothing recorded which branch ran. So "the
totals look wrong" had no answer: a drifted price setting and a vendor
change produce the same symptom and were indistinguishable after the
fact.

Nullable and deliberately not backfilled. Rows written before this
revision genuinely do not know which branch priced them, and stamping
them with a guess is exactly the false certainty the column exists to
remove. NULL means "written before H4", and the constraint admits it.

Reversible and cheap both ways; no data is lost on downgrade beyond the
provenance itself.

Revision ID: b2d5f8a13c47
Revises: a1c4e7b90d63
Create Date: 2026-09-22 17:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2d5f8a13c47'
down_revision: Union[str, Sequence[str], None] = 'a1c4e7b90d63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('spend_ledger', sa.Column('cost_source', sa.String(), nullable=True))
    op.create_check_constraint(
        'ck_spend_ledger_cost_source',
        'spend_ledger',
        "cost_source is null or cost_source in ('vendor', 'computed')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_spend_ledger_cost_source', 'spend_ledger', type_='check')
    op.drop_column('spend_ledger', 'cost_source')
