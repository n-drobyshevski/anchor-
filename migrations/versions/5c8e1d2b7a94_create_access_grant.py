"""create access_grant: opt-in, expiring read grants for an outside assistant

The /grok flow (docs/grok-access.md) lets the user hand grok.com a
capability URL onto a read-only MCP endpoint. Each press of
[Разрешить] writes one row here. Only the sha256 of the capability
token is stored; the token is shown to the user once and is not
recoverable from the database.

Also adds `debug.access_grant` for the `anchor_debug` role, without
the hash: whether and when a grant was used is operational metadata,
the hash is not.

Reversible. Downgrade drops every grant, which revokes them all.

Revision ID: 5c8e1d2b7a94
Revises: 9e4b2c7a1f05
Create Date: 2026-09-24 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '5c8e1d2b7a94'
down_revision: Union[str, Sequence[str], None] = '9e4b2c7a1f05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'access_grant',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('token_sha256', sa.String(), nullable=False),
        sa.Column('scopes', postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column('dialog_days', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('use_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('last_notified_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scopes <@ array['memory', 'journal', 'dialogs', 'state']::varchar[] "
            "and cardinality(scopes) > 0",
            name='ck_access_grant_scopes',
        ),
        sa.CheckConstraint('expires_at > created_at', name='ck_access_grant_expiry'),
        sa.CheckConstraint(
            'dialog_days is null or dialog_days between 1 and 365',
            name='ck_access_grant_dialog_days',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_sha256'),
    )

    op.execute(
        'CREATE VIEW debug.access_grant AS SELECT id, scopes, dialog_days, created_at, '
        'expires_at, revoked_at, last_used_at, use_count, last_notified_at '
        'FROM public.access_grant'
    )
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anchor_debug') THEN
                GRANT SELECT ON debug.access_grant TO anchor_debug;
            END IF;
        END
        $$
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP VIEW debug.access_grant')
    op.drop_table('access_grant')
