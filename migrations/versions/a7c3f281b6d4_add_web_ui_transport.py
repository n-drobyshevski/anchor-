"""add web ui transport: web_update, web_update_seq, web_session

Track 1 of the web-chat plan (docs in /tmp scratchpad, design section 8).
Lays the schema the synthetic-update trick needs: a table that names
which `telegram_update` rows are web-origin and carries POST /api/send's
idempotency key, the sequence that mints negative web update_ids, and
the session table the web login issues cookies against.

**Why this no longer touches `telegram_update` at all.** The first cut
of this migration added `source`/`client_key` columns and two CHECK
constraints directly onto `telegram_update` via `ALTER TABLE`. An ALTER
on that table takes an ACCESS EXCLUSIVE lock, which queues behind
whatever the *previous* deploy's worker is doing to that same hot table
and blocks every new query on it until it either runs or the migration's
`lock_timeout` (migrations/env.py, 30s) trips -- precisely the failure
mode that hung the first Phase 6 deploy on a `telegram_update` ALTER
(migration f7da7c8741fd, commits 2cd24c2/068e7e3). Rather than run that
risk twice, origin is derived instead from the *sign* of `update_id`:
Telegram's own ids are always non-negative, and every web-origin id
comes from `web_update_seq` below and is always negative
(app/db/queue.py's `enqueue_web`), so `update_id < 0` is exactly
"web-origin" with nothing to migrate on the existing table at all.

**Why `web_update` carries no foreign key to `telegram_update`.** A
foreign key takes a SHARE ROW EXCLUSIVE lock on the *referenced* table
at creation time -- weaker than ACCESS EXCLUSIVE, but still enough to
block concurrent DDL and queue behind a busy table, the same class of
risk this rework exists to avoid. `web_update.update_id` matches a
`telegram_update.update_id` by application-level convention only
(`app/db/queue.py`'s `enqueue_web` writes both rows in one transaction),
never by a database constraint. `app/core/purge.py`'s CASCADE-free
TRUNCATE already treats every table in `PURGED_TABLES` as truncated
together in one statement regardless of FK direction, so no code path
here depends on the database enforcing this relationship either.

**Why `client_key` is a nullable column with a partial unique index**,
not a plain unique column. Telegram-origin rows never carry one (there
is no `telegram_update` row without a matching `web_update` row for
web-origin ones, and no `web_update` row at all for Telegram-origin
ones), and a plain unique index treats every NULL as distinct anyway --
but a partial index (`WHERE client_key IS NOT NULL`) says that in the
schema itself rather than relying on NULL semantics an unfamiliar reader
would have to look up. app/db/queue.py's enqueue_web() conflicts on
exactly this index, making a retried POST /api/send with the same
client_key idempotent: same key in, same update_id out.

**Why `web_session.token_hash` is the primary key**, not a surrogate id.
The only query this table ever serves is "does this cookie's hashed
token name a live session" -- a surrogate id would be a second key
nothing reads by. Only sha256(token) is ever stored, never the token
itself (design section 4), the same shape as TELEGRAM_SECRET_TOKEN's own
hmac.compare_digest check in app/tg/webhook.py.

Every statement here is a `CREATE` (table, sequence, or a plain,
non-partial index on a brand-new table) -- there is no `ALTER` on an
existing table anywhere in this migration, and the downgrade only ever
drops what this migration created.

Revision ID: a7c3f281b6d4
Revises: f7da7c8741fd
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7c3f281b6d4'
down_revision: Union[str, Sequence[str], None] = 'f7da7c8741fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(sa.text('CREATE SEQUENCE web_update_seq'))
    op.create_table(
        'web_update',
        sa.Column('update_id', sa.BigInteger(), nullable=False, autoincrement=False),
        sa.Column('client_key', sa.String(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('update_id'),
    )
    op.create_index(
        'uq_web_update_client_key',
        'web_update',
        ['client_key'],
        unique=True,
        postgresql_where=sa.text('client_key IS NOT NULL'),
    )
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
    op.drop_index('uq_web_update_client_key', table_name='web_update')
    op.drop_table('web_update')
    op.execute(sa.text('DROP SEQUENCE web_update_seq'))
