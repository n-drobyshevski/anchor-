"""the code window (CW1): code_window and code_read

anchor-code-window-plan.md section 4.2, milestone CW1. Two tables:

- `code_window`: one row per window the user asked for from Telegram
  (`/vault code on`). `pending` until «Открыть», then `open` with
  `opened_at` and `expires_at`, then `closed` with `closed_at` and a
  `closed_reason` (`expired`, `off`, `replaced`, `delete`). `roots`
  holds the folders the user typed into Telegram (already in
  `message`), never a note's path. A partial unique index keeps at most
  one row `open`.
- `code_read`: one row per successful `tree` or `file` call through a
  window: the route, an entry count (or 1) and the bytes relayed.
  Counts only.

Neither table ever holds a path, a title or note text: the bot relays
what vaultd answers and forgets it (plan section 6).

**Debug views** (granted to `anchor_debug` here, as b8d24f6e0a17
explains, and skipped when the role is absent): `debug.code_window`
without `roots` -- `n_roots` instead, so a folder name the user typed
never reaches the debug role -- and `debug.code_read` whole.

Reversible. Downgrade drops the views, then the tables.

Revision ID: f3b8d1c6a2e9
Revises: d8a4c6e2f175
Create Date: 2026-11-16 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f3b8d1c6a2e9'
down_revision: Union[str, Sequence[str], None] = 'd8a4c6e2f175'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'

# Explicit column lists, never `*` (9e4b2c7a1f05's rule).
CODE_DEBUG_VIEWS: dict[str, str] = {
    'code_window': (
        'id, status, jsonb_array_length(roots) as n_roots, ttl_s, created_at, opened_at, '
        'expires_at, closed_at, closed_reason, notified_at'
    ),
    'code_read': 'id, window_id, at, route, files, bytes',
}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'code_window',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('roots', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('ttl_s', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_reason', sa.String(), nullable=True),
        sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('pending', 'open', 'closed')", name='ck_code_window_status'),
        sa.CheckConstraint(
            "closed_reason is null or closed_reason in ('expired', 'off', 'replaced', 'delete')",
            name='ck_code_window_closed_reason',
        ),
        sa.CheckConstraint('ttl_s in (3600, 21600, 86400)', name='ck_code_window_ttl'),
        sa.CheckConstraint(
            "jsonb_typeof(roots) = 'array' and jsonb_array_length(roots) <= 8",
            name='ck_code_window_roots',
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (opened_at is null) and (opened_at is null) = (expires_at is null)",
            name='ck_code_window_opened',
        ),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at is not null)"
            " and (closed_at is null) = (closed_reason is null)",
            name='ck_code_window_closed',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ux_code_window_open',
        'code_window',
        ['status'],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        'code_read',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('window_id', sa.Integer(), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('route', sa.String(), nullable=False),
        sa.Column('files', sa.Integer(), nullable=False),
        sa.Column('bytes', sa.BigInteger(), nullable=False),
        sa.CheckConstraint("route in ('tree', 'file')", name='ck_code_read_route'),
        sa.CheckConstraint('files >= 0 and bytes >= 0', name='ck_code_read_counts'),
        sa.ForeignKeyConstraint(['window_id'], ['code_window.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_code_read_at', 'code_read', ['at'])
    op.create_index('ix_code_read_window_id', 'code_read', ['window_id'])

    for name, columns in CODE_DEBUG_VIEWS.items():
        op.execute(f'CREATE VIEW debug.{name} AS SELECT {columns} FROM public.{name}')
    grants = '\n'.join(
        f'GRANT SELECT ON debug.{name} TO {DEBUG_ROLE};' for name in CODE_DEBUG_VIEWS
    )
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                {grants}
            END IF;
        END
        $$
    """)


def downgrade() -> None:
    """Downgrade schema."""
    for name in CODE_DEBUG_VIEWS:
        op.execute(f'DROP VIEW IF EXISTS debug.{name}')
    op.drop_index('ix_code_read_window_id', table_name='code_read')
    op.drop_index('ix_code_read_at', table_name='code_read')
    op.drop_table('code_read')
    op.drop_index('ux_code_window_open', table_name='code_window')
    op.drop_table('code_window')
