"""the lens in idle reflect (L5): reflect rounds, their idle run, and the
lens columns on notebook entries

anchor-lens-plan.md sections 7, 10 and 12, milestone L5, as the L5 spec
section 2 settles it with the owner's decisions. The daily idle reflect
may now pick lens notes for itself (app/core/idle/reflect_lens.py) and
let a grounding call rephrase the thread entries of its draft on them;
every such round is recorded like the weekly review's.

- `ck_lens_round_consumer` widens to `('review', 'reflect')`, as
  c6d2e8a4f917 foresaw.
- `lens_round.idle_run_id`: the idle run a reflect round served, SET
  NULL (not CASCADE) like `lens_garden_run.idle_run_id`, and indexed. A
  round must outlive idle pruning: `rounds_since_used` is computed from
  the rounds, and the rounds are plan section 13's audit trail of what
  the selector reached for.
- `ck_lens_round_link`: a review round never points at an idle run,
  and a reflect round never at a weekly review.
- `notebook_entry.lens_round_id` (INTEGER, as `lens_round.id` is; SET
  NULL, so a purged or deleted round never takes an entry with it) and
  `notebook_entry.lens_note_ids` (`INTEGER[] NOT NULL DEFAULT '{}'`):
  which round a grounded entry came out of and which lens notes it
  rests on. The ids are `lens_note` ids, not a foreign key, as on
  `review_proposal`: a note that leaves the lens leaves its id behind.
  Existing rows get `'{}'` and NULL -- the state of every entry the
  lens never touched.

**Unchanged:** `lens.rounds(n)` (it already returns every consumer's
rounds, now reflect's too, with no rationale: reflect stores none) and
`debug.lens_round`, whose explicit column list leaves `idle_run_id`
out (there is no `idle_run` view to join it to). No debug view carries
notebook entries.

Reversible. Downgrade drops the two notebook columns, deletes the
reflect rounds, and restores the constraints.

Revision ID: 3d3efa0cbc9a
Revises: e9a4c2f7b1d8
Create Date: 2026-09-30 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3d3efa0cbc9a'
down_revision: Union[str, Sequence[str], None] = 'e9a4c2f7b1d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSUMER_BEFORE = "consumer in ('review')"
CONSUMER_AFTER = "consumer in ('review', 'reflect')"
LINK = (
    "(consumer = 'review' and idle_run_id is null) "
    "or (consumer = 'reflect' and weekly_review_id is null)"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('ck_lens_round_consumer', 'lens_round', type_='check')
    op.create_check_constraint('ck_lens_round_consumer', 'lens_round', CONSUMER_AFTER)

    op.add_column('lens_round', sa.Column('idle_run_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        'fk_lens_round_idle_run_id',
        'lens_round',
        'idle_run',
        ['idle_run_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index('ix_lens_round_idle_run_id', 'lens_round', ['idle_run_id'])
    op.create_check_constraint('ck_lens_round_link', 'lens_round', LINK)

    op.add_column('notebook_entry', sa.Column('lens_round_id', sa.Integer(), nullable=True))
    op.add_column(
        'notebook_entry',
        sa.Column(
            'lens_note_ids',
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        'fk_notebook_entry_lens_round_id',
        'notebook_entry',
        'lens_round',
        ['lens_round_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_notebook_entry_lens_round_id', 'notebook_entry', type_='foreignkey')
    op.drop_column('notebook_entry', 'lens_note_ids')
    op.drop_column('notebook_entry', 'lens_round_id')

    op.execute("DELETE FROM lens_round WHERE consumer = 'reflect'")
    op.drop_constraint('ck_lens_round_link', 'lens_round', type_='check')
    op.drop_index('ix_lens_round_idle_run_id', table_name='lens_round')
    op.drop_constraint('fk_lens_round_idle_run_id', 'lens_round', type_='foreignkey')
    op.drop_column('lens_round', 'idle_run_id')
    op.drop_constraint('ck_lens_round_consumer', 'lens_round', type_='check')
    op.create_check_constraint('ck_lens_round_consumer', 'lens_round', CONSUMER_BEFORE)
