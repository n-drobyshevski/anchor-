"""study_card status 'forgotten': /forget of an adopted technique

Milestone 5b (phase-5 plan section 6), fixing a phase-4 bug.
`study_card.memory_id` has no ON DELETE rule, so deleting the memory an
adopted card points at failed on the foreign key: /forget of an adopted
technique raised. From here, `memory.hard_delete` first marks such a
card `forgotten` with a null memory_id. `ck_study_card_adopted_has_memory`
already allows a null memory_id for every status but `adopted`, so only
the status list widens.

Reversible, provided no card is `forgotten`: the downgrade maps them to
`expired` first, the nearest status the old list has.

Revision ID: c3e9a71f4d20
Revises: b8d24f6e0a17
Create Date: 2026-09-25 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3e9a71f4d20'
down_revision: Union[str, Sequence[str], None] = 'b8d24f6e0a17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BEFORE = "status in ('pending', 'adopted', 'rejected', 'hidden', 'expired')"
_AFTER = "status in ('pending', 'adopted', 'rejected', 'hidden', 'expired', 'forgotten')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('ck_study_card_status', 'study_card', type_='check')
    op.create_check_constraint('ck_study_card_status', 'study_card', _AFTER)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE study_card SET status = 'expired' WHERE status = 'forgotten'")
    op.drop_constraint('ck_study_card_status', 'study_card', type_='check')
    op.create_check_constraint('ck_study_card_status', 'study_card', _BEFORE)
