"""content-free debug views over the vault tables

Milestone 8a (phase-8 plan section 11). Same rules as 9e4b2c7a1f05:
explicit column lists, no free text, lengths where size is diagnostic.

- `debug.vault_file` has **no path**: a file name the user chose is
  content, and so is a note's title. It carries `seen` (whether a hash
  was ever recorded) instead of the hashes themselves, since a hash of
  a short fact confirms a guess at its text.
- `debug.vault_hold` has **no payload**, only how many files a
  mass-delete hold covers.
- `debug.vault_chunk` has lengths only.
- `debug.vault_status` is the whole row; it holds only timestamps.

**This revision grants its own views.** 9e4b2c7a1f05 ran `GRANT SELECT
ON ALL TABLES IN SCHEMA debug`, which covers the views that existed at
that moment and nothing created afterwards. Without the grant below,
`anchor_debug` could see these views in the catalogue and read none of
them.

Reversible.

Revision ID: b8d24f6e0a17
Revises: a5c1e0d9b3f2
Create Date: 2026-09-25 12:10:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8d24f6e0a17'
down_revision: Union[str, Sequence[str], None] = 'a5c1e0d9b3f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'

VAULT_DEBUG_VIEWS: dict[str, str] = {
    'vault_file': (
        'id, role, state, reason, memory_id, local_date, hold_id, '
        'disk_sha256 is not null as seen, render_digest is not null as rendered, '
        'missing_since, created_at, updated_at'
    ),
    'vault_hold': (
        "id, kind, status, jsonb_array_length(coalesce(payload -> 'file_ids', '[]'::jsonb)) "
        'as file_count, tg_message_id is not null as asked, created_at, decided_at'
    ),
    'vault_chunk': 'id, file_id, ord, char_length(heading) as heading_len, char_length(text) as text_len',
    'vault_status': 'id, last_ok_at, last_unavailable_at, ob_running_since, forgets_window',
}


def upgrade() -> None:
    """Upgrade schema."""
    for name, columns in VAULT_DEBUG_VIEWS.items():
        op.execute(f'CREATE VIEW debug.{name} AS SELECT {columns} FROM public.{name}')
    grants = '\n'.join(
        f'GRANT SELECT ON debug.{name} TO {DEBUG_ROLE};' for name in VAULT_DEBUG_VIEWS
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
    for name in VAULT_DEBUG_VIEWS:
        op.execute(f'DROP VIEW IF EXISTS debug.{name}')
