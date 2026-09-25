"""create notebook_entry

Milestone 5b (phase-5 plan sections 3 and 6; implementation plan's
"Files" list). Anchor's own working notes: intentions (user- or
review-written only), observations and open threads (written by the
`notebook_reflect` job), all visible to the user through `/mind` and
closable by them regardless of who wrote them.

Two checks beyond the plan's own SQL, both structural rather than
behavioural -- the ownership rule itself (Anchor cannot close or edit a
user- or review-authored row) is enforced in app/core/notebook.py's
`validate()`, not here, because it depends on *who is asking*, which a
CHECK constraint cannot see.

`ck_notebook_entry_closed_consistent` ties `active` to `closed_by`/
`closed_at` together: an active row has neither, a closed row has both.
Without it, a bug that flips `active` to false without also stamping
`closed_by` would leave a closed entry that /mind's "closed by anchor/
user/expiry" reasoning cannot explain, and the mirror bug -- a `closed_
by` surviving a re-open that never happens in this schema anyway --
would silently misreport who closed a row that is not actually closed.

`scene_id` references `scene.id` with `ON DELETE SET NULL`, not CASCADE
and not a plain FK: app/core/purge.py's /delete TRUNCATEs `notebook_
entry` ahead of `scene` in the same statement (so the FK never fires
there), but nothing else in the schema deletes a `scene` row on its
own, and if that ever changes, the notebook's idempotency marker
(`run_notebook_reflect` checks for an existing row by `scene_id`) must
outlive the scene it was written about rather than vanish with it.

Revision ID: a3d97e6c1b2f
Revises: f1a2c8e6d904
Create Date: 2026-09-22 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a3d97e6c1b2f'
down_revision: Union[str, Sequence[str], None] = 'f1a2c8e6d904'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'notebook_entry',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('closed_by', sa.String(), nullable=True),
        sa.Column('scene_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind in ('intention', 'observation', 'open_thread')",
            name='ck_notebook_entry_kind',
        ),
        sa.CheckConstraint('char_length("text") <= 240', name='ck_notebook_entry_text_length'),
        sa.CheckConstraint(
            "source in ('anchor', 'user', 'review')", name='ck_notebook_entry_source'
        ),
        sa.CheckConstraint(
            "closed_by is null or closed_by in ('anchor', 'user', 'expiry')",
            name='ck_notebook_entry_closed_by',
        ),
        sa.CheckConstraint(
            "(active and closed_by is null and closed_at is null) or "
            "(not active and closed_by is not null and closed_at is not null)",
            name='ck_notebook_entry_closed_consistent',
        ),
        sa.ForeignKeyConstraint(['scene_id'], ['scene.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_notebook_entry_active_kind', 'notebook_entry', ['active', 'kind'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_notebook_entry_active_kind', table_name='notebook_entry')
    op.drop_table('notebook_entry')
