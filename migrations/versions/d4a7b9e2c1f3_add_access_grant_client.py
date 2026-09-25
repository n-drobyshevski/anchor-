"""add access_grant.client: whose read grant this is

Connector milestone C1 (anchor-claude-connector-plan.md sections 7 and
9). A grant row will belong to one of two outside assistants: `grok`
(a capability URL, so it has a token hash) or, from C2, `claude` (a
window on an OAuth connection, so it has none). This adds the column
and the pairing, and nothing else:

- `client text not null default 'grok'`, CHECK `in ('grok','claude')`;
  every existing row is Grok's, which the default says.
- CHECK `(client = 'grok') = (token_sha256 is not null)`.

`token_sha256` stays NOT NULL here, so together the two checks refuse
a `claude` row until C2 relaxes it in the same migration that creates
`oauth_connection` and adds `access_grant.connection_id` (which has
nothing to reference yet). The database, not the code, keeps a Claude
grant from existing before its OAuth server does
(docs/decisions.md, "C1 -- one column").

`debug.access_grant` gains `client`: which assistant a grant was for
is operational metadata, like its scopes.

Reversible.

Revision ID: d4a7b9e2c1f3
Revises: c3e8f5a1d2b6
Create Date: 2026-09-25 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4a7b9e2c1f3'
down_revision: Union[str, Sequence[str], None] = 'c3e8f5a1d2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_VIEW_COLUMNS = (
    'id, scopes, dialog_days, created_at, expires_at, revoked_at, last_used_at, '
    'use_count, last_notified_at'
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'access_grant',
        sa.Column('client', sa.String(), server_default=sa.text("'grok'"), nullable=False),
    )
    op.create_check_constraint(
        'ck_access_grant_client', 'access_grant', "client in ('grok', 'claude')"
    )
    op.create_check_constraint(
        'ck_access_grant_client_token',
        'access_grant',
        "(client = 'grok') = (token_sha256 is not null)",
    )
    # Appending a column is what CREATE OR REPLACE VIEW allows, and it
    # keeps the view's existing grant.
    op.execute(
        f'CREATE OR REPLACE VIEW debug.access_grant AS SELECT {OLD_VIEW_COLUMNS}, client '
        'FROM public.access_grant'
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP VIEW debug.access_grant')
    op.execute(f'CREATE VIEW debug.access_grant AS SELECT {OLD_VIEW_COLUMNS} FROM public.access_grant')
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anchor_debug') THEN
                GRANT SELECT ON debug.access_grant TO anchor_debug;
            END IF;
        END
        $$
    """)
    op.drop_constraint('ck_access_grant_client_token', 'access_grant', type_='check')
    op.drop_constraint('ck_access_grant_client', 'access_grant', type_='check')
    op.drop_column('access_grant', 'client')
