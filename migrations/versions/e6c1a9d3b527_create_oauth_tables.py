"""create the OAuth tables for the Claude connector; windows on access_grant

Connector milestone C2 (anchor-claude-connector-plan.md sections 5 and
7; the registration shape pinned by the dry run, docs/decisions.md
"C2 -- the dry run's answers"):

- `oauth_connection`: claude.ai's one connection. A partial unique
  index on `(true) WHERE revoked_at IS NULL` makes a second active row
  impossible; `expires_at` is absolute.
- `oauth_request`: approved authorization requests only (pending ones
  live in memory). Holds the sha256 of a 60-second code and what it is
  bound to.
- `oauth_token`: access and refresh tokens, sha256 only, with the
  rotation marker `replaced_at`.
- There is no `oauth_client` table: claude.ai registers by CIMD, and
  the one accepted client id is a constant in code.
- `access_grant` gains `connection_id`; `token_sha256` becomes
  nullable, so a `claude` row (a window) can exist, and the new pairing
  check `(client = 'claude') = (connection_id is not null)` joins C1's
  `(client = 'grok') = (token_sha256 is not null)`.
- Debug views: `debug.oauth_connection` (id and timestamps) and
  `debug.oauth_request` (id, status, timestamps, connection id); never
  a client id, redirect, challenge, scope or hash. `debug.access_grant`
  gains `connection_id`. Each is granted here.

Reversible. The downgrade drops the three tables and every Claude
window, then restores `token_sha256 NOT NULL`.

Revision ID: e6c1a9d3b527
Revises: d4a7b9e2c1f3
Create Date: 2026-09-25 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e6c1a9d3b527'
down_revision: Union[str, Sequence[str], None] = 'd4a7b9e2c1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'
GRANT_VIEW_COLUMNS = (
    'id, scopes, dialog_days, created_at, expires_at, revoked_at, last_used_at, '
    'use_count, last_notified_at, client'
)


def _grant(views: Sequence[str]) -> None:
    grants = '\n'.join(f'GRANT SELECT ON debug.{name} TO {DEBUG_ROLE};' for name in views)
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                {grants}
            END IF;
        END
        $$
    """)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'oauth_connection',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('client_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('expires_at > created_at', name='ck_oauth_connection_expiry'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_oauth_connection_active',
        'oauth_connection',
        [sa.text('(true)')],
        unique=True,
        postgresql_where=sa.text('revoked_at is null'),
    )
    op.create_table(
        'oauth_request',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('client_id', sa.String(), nullable=False),
        sa.Column('redirect_uri', sa.String(), nullable=False),
        sa.Column('resource', sa.String(), nullable=False),
        sa.Column('scope', sa.String(), nullable=False),
        sa.Column('code_challenge', sa.String(), nullable=False),
        sa.Column('code_sha256', sa.String(), nullable=False),
        sa.Column('code_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('connection_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('approved', 'redeemed', 'expired')", name='ck_oauth_request_status'
        ),
        sa.ForeignKeyConstraint(['connection_id'], ['oauth_connection.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code_sha256'),
    )
    op.create_table(
        'oauth_token',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('connection_id', sa.BigInteger(), nullable=False),
        sa.Column('request_id', sa.BigInteger(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('token_sha256', sa.String(), nullable=False),
        sa.Column('audience', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('replaced_by', sa.BigInteger(), nullable=True),
        sa.Column('replaced_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind in ('access', 'refresh')", name='ck_oauth_token_kind'),
        sa.CheckConstraint('expires_at > created_at', name='ck_oauth_token_expiry'),
        sa.ForeignKeyConstraint(['connection_id'], ['oauth_connection.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['request_id'], ['oauth_request.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['replaced_by'], ['oauth_token.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_sha256'),
    )
    op.create_index('ix_oauth_token_connection_id', 'oauth_token', ['connection_id'])

    op.add_column('access_grant', sa.Column('connection_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        'access_grant_connection_id_fkey',
        'access_grant',
        'oauth_connection',
        ['connection_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.alter_column('access_grant', 'token_sha256', existing_type=sa.String(), nullable=True)
    op.create_check_constraint(
        'ck_access_grant_client_connection',
        'access_grant',
        "(client = 'claude') = (connection_id is not null)",
    )

    op.execute(
        'CREATE VIEW debug.oauth_connection AS SELECT id, created_at, expires_at, '
        'last_used_at, revoked_at FROM public.oauth_connection'
    )
    op.execute(
        'CREATE VIEW debug.oauth_request AS SELECT id, status, code_expires_at, '
        'connection_id, created_at FROM public.oauth_request'
    )
    op.execute(
        f'CREATE OR REPLACE VIEW debug.access_grant AS SELECT {GRANT_VIEW_COLUMNS}, '
        'connection_id FROM public.access_grant'
    )
    _grant(['oauth_connection', 'oauth_request'])


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP VIEW debug.oauth_request')
    op.execute('DROP VIEW debug.oauth_connection')
    op.execute('DROP VIEW debug.access_grant')
    op.execute(
        f'CREATE VIEW debug.access_grant AS SELECT {GRANT_VIEW_COLUMNS} FROM public.access_grant'
    )
    _grant(['access_grant'])

    op.execute("DELETE FROM access_grant WHERE client = 'claude'")
    op.drop_constraint('ck_access_grant_client_connection', 'access_grant', type_='check')
    op.alter_column('access_grant', 'token_sha256', existing_type=sa.String(), nullable=False)
    op.drop_constraint('access_grant_connection_id_fkey', 'access_grant', type_='foreignkey')
    op.drop_column('access_grant', 'connection_id')

    op.drop_index('ix_oauth_token_connection_id', table_name='oauth_token')
    op.drop_table('oauth_token')
    op.drop_table('oauth_request')
    op.drop_index('uq_oauth_connection_active', table_name='oauth_connection')
    op.drop_table('oauth_connection')
