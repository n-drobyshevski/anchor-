"""add Claude's write switch and its changeset ledger (W2b)

anchor-claude-write-plan.md sections 5 and 7; docs/decisions.md "W1 --
decisions settled":

- `oauth_connection.library_write boolean not null default false`: the
  second standing switch, `/claude library write on|off`
  (app/tg/claude.py). Off by default; needs `library_read` on to turn
  on; turning `library_read` off also turns this off
  (app/web/oauth_store.py's `set_library`).
- `claude_changeset`: no paths, no text -- ids, counts and times only.
  `kind` is `'write'` or `'undo'`. `created`/`renamed` count, within a
  write changeset, how many of its files were a create or a rename
  (the digest's own markers -- `GET /v1/changes` does not itself carry
  that distinction). Cascades from `oauth_connection`.
- Debug view: `debug.oauth_connection` gains `library_write`, same
  reasoning as `library_read`'s own line in aae4f596191d. No debug
  view for `claude_changeset` -- content-free already, no existing
  debugging story needs it, same call as `claude_library_read`.

Reversible.

Revision ID: b1c4d8e29f6a
Revises: aae4f596191d
Create Date: 2026-09-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b1c4d8e29f6a'
down_revision: Union[str, Sequence[str], None] = 'aae4f596191d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'


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
        sa.Column('library_write', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.execute(
        'CREATE OR REPLACE VIEW debug.oauth_connection AS SELECT id, created_at, expires_at, '
        'last_used_at, revoked_at, library_read, library_write FROM public.oauth_connection'
    )
    _grant(['oauth_connection'])

    op.create_table(
        'claude_changeset',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('connection_id', sa.BigInteger(), nullable=False),
        sa.Column('vault_ref', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('files', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('bytes', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('refused', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('renamed', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_write_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('undone_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind in ('write', 'undo')", name='ck_claude_changeset_kind'),
        sa.CheckConstraint('files >= 0', name='ck_claude_changeset_files'),
        sa.CheckConstraint('bytes >= 0', name='ck_claude_changeset_bytes'),
        sa.CheckConstraint('refused >= 0', name='ck_claude_changeset_refused'),
        sa.CheckConstraint('created >= 0', name='ck_claude_changeset_created'),
        sa.CheckConstraint('renamed >= 0', name='ck_claude_changeset_renamed'),
        sa.ForeignKeyConstraint(['connection_id'], ['oauth_connection.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_claude_changeset_connection_id', 'claude_changeset', ['connection_id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_claude_changeset_connection_id', table_name='claude_changeset')
    op.drop_table('claude_changeset')

    op.execute('DROP VIEW debug.oauth_connection')
    op.execute(
        'CREATE VIEW debug.oauth_connection AS SELECT id, created_at, expires_at, '
        'last_used_at, revoked_at, library_read FROM public.oauth_connection'
    )
    _grant(['oauth_connection'])
    op.drop_column('oauth_connection', 'library_write')
