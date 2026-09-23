"""add web ui transport: telegram_update.source/client_key, web_update_seq, web_session

Track 1 of the web-chat plan (docs in /tmp scratchpad, design section 8).
Lays the schema the synthetic-update trick needs: a `source` column that
tells the worker which `Bot` to feed a row to, a `client_key` for
POST /api/send idempotency, the sequence that mints negative web
update_ids, and the session table the web login issues cookies against.

**Why a CHECK on the sign of update_id, not just a CHECK on `source`.**
`(source = 'web') = (update_id < 0)` ties the two together at the
database level rather than trusting every future writer to keep them in
sync by convention. Telegram's own update_id is always non-negative, so
this is free for existing rows (source defaults to 'telegram', and every
row already satisfies update_id >= 0); a web row that ever got created
with a non-negative id, or a telegram row relabeled 'web', is rejected
by Postgres rather than silently reordering the claim queue (see
app/db/queue.py's UPDATE_SPEC.order_by, which now sorts by
(created_at, update_id) rather than update_id alone -- the ordering fix
the design's second adversarial review called for).

**Why `client_key` is a nullable column with a partial unique index**,
not a plain unique column. Telegram-origin rows never carry one (NULL),
and a plain unique index treats every NULL as distinct anyway -- but a
partial index (`WHERE client_key IS NOT NULL`) says that in the schema
itself rather than relying on NULL semantics an unfamiliar reader would
have to look up. app/db/queue.py's enqueue_web() conflicts on exactly
this index, making a retried POST /api/send with the same client_key
idempotent: same key in, same update_id out.

**Why `web_session.token_hash` is the primary key**, not a surrogate id.
The only query this table ever serves is "does this cookie's hashed
token name a live session" -- a surrogate id would be a second key
nothing reads by. Only sha256(token) is ever stored, never the token
itself (design section 4), the same shape as TELEGRAM_SECRET_TOKEN's own
hmac.compare_digest check in app/tg/webhook.py.

Downgrade reverses all of it. A downgrade run while web rows exist in
`telegram_update` will fail closed: dropping `source`/`client_key`
after `web_session` and the sequence is deliberately left as a
formality for a table nothing else references (the CASCADE-free
TRUNCATE precedent in app/core/purge.py is the same instinct -- a
schema change that can quietly lose data should fail loudly instead).

Revision ID: a7c3f281b6d4
Revises: d1b83f6c204e
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7c3f281b6d4'
down_revision: Union[str, Sequence[str], None] = 'd1b83f6c204e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'telegram_update',
        sa.Column('source', sa.String(), server_default=sa.text("'telegram'"), nullable=False),
    )
    op.create_check_constraint(
        'ck_telegram_update_source', 'telegram_update', "source in ('telegram', 'web')"
    )
    op.create_check_constraint(
        'ck_telegram_update_source_sign',
        'telegram_update',
        "(source = 'web') = (update_id < 0)",
    )
    op.add_column('telegram_update', sa.Column('client_key', sa.String(), nullable=True))
    op.create_index(
        'uq_telegram_update_client_key',
        'telegram_update',
        ['client_key'],
        unique=True,
        postgresql_where=sa.text('client_key IS NOT NULL'),
    )
    op.execute(sa.text('CREATE SEQUENCE web_update_seq'))
    op.create_table(
        'web_session',
        sa.Column('token_hash', sa.LargeBinary(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('token_hash'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('web_session')
    op.execute(sa.text('DROP SEQUENCE web_update_seq'))
    op.drop_index('uq_telegram_update_client_key', table_name='telegram_update')
    op.drop_column('telegram_update', 'client_key')
    op.drop_constraint('ck_telegram_update_source_sign', 'telegram_update', type_='check')
    op.drop_constraint('ck_telegram_update_source', 'telegram_update', type_='check')
    op.drop_column('telegram_update', 'source')
