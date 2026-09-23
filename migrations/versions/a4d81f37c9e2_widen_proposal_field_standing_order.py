"""widen ck_proposal_field with standing_order

Milestone 5c. The extractor's schema (app/core/extract.py) can now emit
a `standing_order` proposal item, and `proposal.FIELDS` is widened to
match -- even though `_apply()` never actually inserts a `Proposal` row
for it (it routes to app/core/orders.propose() instead, which writes
its own `standing_order` row: see that module and app/db/models.py's
`Proposal` docstring for why). The widening keeps this constraint in
step with the enum it mirrors regardless of that routing choice, the
same "additive, nullable-safe" move `e7b1c4a9d803` made for
`ck_safety_event_kind`.

Revision ID: a4d81f37c9e2
Revises: f8c2b6e14a97
Create Date: 2026-09-23 09:05:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4d81f37c9e2'
down_revision: Union[str, Sequence[str], None] = 'f8c2b6e14a97'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "field in ('due_action', 'focus_on', 'rule')"
_NEW = "field in ('due_action', 'focus_on', 'rule', 'standing_order')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('ck_proposal_field', 'proposal', type_='check')
    op.create_check_constraint('ck_proposal_field', 'proposal', _NEW)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_proposal_field', 'proposal', type_='check')
    op.create_check_constraint('ck_proposal_field', 'proposal', _OLD)
