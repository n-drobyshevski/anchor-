"""add claude_changeset.folders and .moves (rev. 3)

anchor-claude-write-plan.md section 14; docs/decisions.md "W2a/W2b" and
BUILD item 8:

- `claude_changeset.folders`: folders created for that changeset
  (create_note or rename_note's destination), summed from vaultd's own
  `folders_created` response field. Caps: FOLDERS_PER_CHANGESET (3),
  FOLDERS_PER_DAY (10).
- `claude_changeset.moves`: files touched by a rename (the moved note
  plus its rewritten backlinks), summed from vaultd's `files_moved`.
  Caps: MOVE_FILES_PER_CHANGESET (20), MOVES_PER_DAY (60). Entirely
  separate from `files`, which now counts content writes only.

Still no path, no text -- ids, counts and times only. Reversible.

Revision ID: 027b3c0b323d
Revises: b1c4d8e29f6a
Create Date: 2026-09-28 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '027b3c0b323d'
down_revision: Union[str, Sequence[str], None] = 'b1c4d8e29f6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'claude_changeset',
        sa.Column('folders', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )
    op.add_column(
        'claude_changeset',
        sa.Column('moves', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )
    op.create_check_constraint('ck_claude_changeset_folders', 'claude_changeset', 'folders >= 0')
    op.create_check_constraint('ck_claude_changeset_moves', 'claude_changeset', 'moves >= 0')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_claude_changeset_moves', 'claude_changeset', type_='check')
    op.drop_constraint('ck_claude_changeset_folders', 'claude_changeset', type_='check')
    op.drop_column('claude_changeset', 'moves')
    op.drop_column('claude_changeset', 'folders')
