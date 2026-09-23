"""add standing_order.review_proposal_id

Milestone 5d. Links a `standing_order` row proposed by the weekly
review back to its `review_proposal` row, so `app/tg/orders.py`'s
`so:a`/`so:r` callbacks can call `review.mark_proposal(id, 'adopted' |
'rejected')` on the review's own row when the order it proposed is
decided -- each module still writes only its own table (`orders.py`
sets this column via its own targeted UPDATE; `review.py` never writes
`standing_order`). `ON DELETE SET NULL`, not CASCADE: purging a review
proposal (there is none -- `review_proposal` is never deleted outside
`/delete`, which purges both tables together) must not silently delete
an order the user is holding.

Revision ID: ab0ad60cd7ec
Revises: af8fefa6dc55
Create Date: 2026-09-23 10:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'ab0ad60cd7ec'
down_revision: Union[str, Sequence[str], None] = 'af8fefa6dc55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('standing_order', sa.Column('review_proposal_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        'fk_standing_order_review_proposal_id',
        'standing_order',
        'review_proposal',
        ['review_proposal_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_standing_order_review_proposal_id', 'standing_order', type_='foreignkey')
    op.drop_column('standing_order', 'review_proposal_id')
