"""widen ck_outbound_kind with weekly_review, ck_safety_event_kind with review

Milestone 5d. The weekly review is a gated outbound kind
(`app/core/outbound_gate.py`'s `WEEKLY_REVIEW`), so `outbound.kind` has
to accept it; its own safety-model analysis call is ledgered as a
`SafetyEvent` of kind `review`, the same H2 shape `notebook`/`distill`/
`search` already are (`e7b1c4a9d803`/`d1b83f6c204e` made this exact
move for those). Both widenings are additive and nullable-safe, so
every existing row stays valid.

Revision ID: af8fefa6dc55
Revises: dcc3e9f3b709
Create Date: 2026-09-23 10:05:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'af8fefa6dc55'
down_revision: Union[str, Sequence[str], None] = 'dcc3e9f3b709'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OUTBOUND_OLD = "kind in ('morning', 'evening_nag', 'silence', 'tick')"
_OUTBOUND_NEW = "kind in ('morning', 'evening_nag', 'silence', 'tick', 'weekly_review')"

_SAFETY_OLD = "kind in ('welfare', 'extractor', 'tick', 'distill', 'search', 'notebook')"
_SAFETY_NEW = (
    "kind in ('welfare', 'extractor', 'tick', 'distill', 'search', 'notebook', 'review')"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('ck_outbound_kind', 'outbound', type_='check')
    op.create_check_constraint('ck_outbound_kind', 'outbound', _OUTBOUND_NEW)
    op.drop_constraint('ck_safety_event_kind', 'safety_event', type_='check')
    op.create_check_constraint('ck_safety_event_kind', 'safety_event', _SAFETY_NEW)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_safety_event_kind', 'safety_event', type_='check')
    op.create_check_constraint('ck_safety_event_kind', 'safety_event', _SAFETY_OLD)
    op.drop_constraint('ck_outbound_kind', 'outbound', type_='check')
    op.create_check_constraint('ck_outbound_kind', 'outbound', _OUTBOUND_OLD)
