"""extend ck_safety_event_kind with notebook

Milestone 5b. `run_notebook_reflect` (app/core/notebook.py) is a sixth
safety-model call of the same shape H2 built this table to catch: a
timeout writes no spend_ledger row, an unparseable reply is a silent
no-op, and a check that has stopped working looks exactly like a
session with nothing to reflect on. `d1b83f6c204e` already widened this
constraint once, for `distill` and `search`; this is the same move for
`notebook`.

Nullable-safe and additive, like that revision: the constraint only
widens, so every existing row stays valid.

Revision ID: e7b1c4a9d803
Revises: a3d97e6c1b2f
Create Date: 2026-09-22 23:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e7b1c4a9d803'
down_revision: Union[str, Sequence[str], None] = 'a3d97e6c1b2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "kind in ('welfare', 'extractor', 'tick', 'distill', 'search')"
_NEW = "kind in ('welfare', 'extractor', 'tick', 'distill', 'search', 'notebook')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('ck_safety_event_kind', 'safety_event', type_='check')
    op.create_check_constraint('ck_safety_event_kind', 'safety_event', _NEW)


def downgrade() -> None:
    """Downgrade schema."""
    # An observability row with no referent -- see d1b83f6c204e, the
    # same reasoning as this table's earlier widening.
    op.execute(sa.text("delete from safety_event where kind = 'notebook'"))
    op.drop_constraint('ck_safety_event_kind', 'safety_event', type_='check')
    op.create_check_constraint('ck_safety_event_kind', 'safety_event', _OLD)
