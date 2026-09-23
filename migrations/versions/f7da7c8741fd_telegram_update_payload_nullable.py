"""allow telegram_update.payload to be null

Milestone 6e (Phase 6 plan section 9.4): the daily retention sweep
(app/core/retention.py's `forget_update_payloads`) nulls out
`telegram_update.payload` -- the raw Telegram envelope, which can carry
message text -- after `UPDATE_PAYLOAD_RETENTION_DAYS`. The column has
been `NOT NULL` since Phase 1 (migrations/versions/
e23dfc90818e_create_telegram_update.py), which the sweep cannot honour
without this relaxation. Row identity, `status`, `attempts` and
`created_at` are all untouched -- only the payload itself is ever
cleared, and only on rows old enough that the queue will never reclaim
them again (`claim()`/`recover_stuck` only ever touch rows stuck for
minutes, not weeks).

Revision ID: f7da7c8741fd
Revises: a339f54e49de
Create Date: 2026-09-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f7da7c8741fd'
down_revision = 'a339f54e49de'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('telegram_update', 'payload', existing_type=sa.dialects.postgresql.JSONB(), nullable=True)


def downgrade() -> None:
    """Downgrade schema.

    Fails if any row's payload was already nulled by the retention
    sweep -- that data is gone and cannot be un-forgotten, which is the
    correct failure mode for a downgrade rather than silently
    backfilling `{}`.
    """
    op.alter_column('telegram_update', 'payload', existing_type=sa.dialects.postgresql.JSONB(), nullable=False)
