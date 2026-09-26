"""add the Claude library (C3): oauth_connection.library_read, the
notes_knowledge scope, and claude_library_read

Connector milestone C3 (anchor-claude-connector-plan.md sections 6 and
9; docs/decisions.md, "C3 -- search_library without the failed
threshold" and "Index knowledge notes only"):

- `oauth_connection.library_read boolean not null default false`: the
  standing switch `/claude library on|off` writes (app/tg/claude.py).
  Not a window -- while true and the connection is alive,
  `search_library` works with no `/claude` window open. A new
  connection starts with it off, which the default says; existing
  connections get the same default on upgrade, i.e. off, matching "a
  new connection starts off" for anything created before this
  migration too.
- `ck_access_grant_scopes` widens to allow `notes_knowledge` alongside
  the four personal scopes, and continues to refuse everything else,
  in particular `notes_personal` -- a personal note must never become
  reachable through a grant's `scopes` array, window-scoped or not.
  Nothing in this codebase writes `notes_knowledge` into a grant's
  `scopes` today (the switch above is what gates `search_library`);
  the CHECK is widened so the literal exists at the database level
  wherever `access_grant.scopes` is reasoned about, per the connector
  plan's "C2 design for how tools/list relates to scopes".
- `claude_library_read (local_date date primary key, count integer)`:
  a content-free daily counter for app/tg/claude.py's once-a-day
  digest. Never a query, a heading or a chunk.
- Debug views: `debug.oauth_connection` gains `library_read` (a
  boolean switch, not a secret). `claude_library_read` gets no debug
  view: it is already content-free and not part of any operational
  debugging story this repo has needed so far.

Reversible.

Revision ID: aae4f596191d
Revises: e6c1a9d3b527
Create Date: 2026-09-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'aae4f596191d'
down_revision: Union[str, Sequence[str], None] = 'e6c1a9d3b527'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'
OLD_SCOPES_CHECK = (
    "scopes <@ array['memory', 'journal', 'dialogs', 'state']::varchar[] "
    "and cardinality(scopes) > 0"
)
NEW_SCOPES_CHECK = (
    "scopes <@ array['memory', 'journal', 'dialogs', 'state', 'notes_knowledge']"
    "::varchar[] and cardinality(scopes) > 0"
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
    op.add_column(
        'oauth_connection',
        sa.Column('library_read', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.execute(
        'CREATE OR REPLACE VIEW debug.oauth_connection AS SELECT id, created_at, expires_at, '
        'last_used_at, revoked_at, library_read FROM public.oauth_connection'
    )
    _grant(['oauth_connection'])

    op.drop_constraint('ck_access_grant_scopes', 'access_grant', type_='check')
    op.create_check_constraint('ck_access_grant_scopes', 'access_grant', NEW_SCOPES_CHECK)

    op.create_table(
        'claude_library_read',
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.CheckConstraint('count >= 0', name='ck_claude_library_read_count'),
        sa.PrimaryKeyConstraint('local_date'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('claude_library_read')

    op.drop_constraint('ck_access_grant_scopes', 'access_grant', type_='check')
    op.create_check_constraint('ck_access_grant_scopes', 'access_grant', OLD_SCOPES_CHECK)

    op.execute('DROP VIEW debug.oauth_connection')
    op.execute(
        'CREATE VIEW debug.oauth_connection AS SELECT id, created_at, expires_at, '
        'last_used_at, revoked_at FROM public.oauth_connection'
    )
    _grant(['oauth_connection'])
    op.drop_column('oauth_connection', 'library_read')
