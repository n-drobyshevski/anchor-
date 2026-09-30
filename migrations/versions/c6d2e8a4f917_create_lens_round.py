"""the weekly review's lens rounds (L2): lens_round, review_proposal's
grounds, and `lens.rounds()`

anchor-lens-plan.md sections 5, 7 and 11, milestone L2. When the lens
is on, the weekly review asks a selector which lens notes bear on the
week, then grounds its proposals in them (app/core/lens_review.py).
Every such round is recorded, whatever became of it:

- `lens_round`: one row per round -- the consumer (only `review` in
  L2; reflect joins in L5 by widening the CHECK), the review it served
  (set once that row exists; cascades with it), the lens version it
  saw (SET NULL: a version row is history, never a reason to lose the
  round), the selected `lens_note` ids in the selector's order, the
  selector's `why` (`rationale`), and the `outcome`: `grounded` (the
  proposals rest on the selection), `empty` (nothing fit this week) or
  `fallback` (a selector or grounding call failed; the review kept its
  first-pass proposals).
- `review_proposal.lens_round_id` and `.lens_note_ids`: which round a
  proposal came out of and which notes it names as its grounds. The
  ids are `lens_note` ids, not a foreign key: a note that leaves the
  lens leaves its id behind, and the card skips it.

`rounds_since_used` in the selector's catalog is computed from
`selected_note_ids` (app/vault/lens.py), so a GIN index is not needed
at this size: a few rounds a month.

**Debug views** (granted to `anchor_debug` here, as b8d24f6e0a17
explains): `debug.lens_round` has every column but `rationale` -- the
selector's own words about the user's week and the notes it picked.
`debug.review_proposal` is new: ids, kind, status, times, the two new
columns, and the proposal's length; never its text or reason.

**Claude Code's door gains `lens.rounds(n)`** (plan section 11): the
last `least(n, 50)` rounds, newest first, with their outcome and the
selected notes' current titles (a note no longer in the lens is
skipped). Never the rationale: it is written from the analysis of the
user's week, so it is conversation-derived and stays with the user
(the Telegram card's «почему эти заметки?»), out of both this function
and `debug.lens_round`. SECURITY DEFINER exactly as e4c7a2d9b1f3's two functions: a
pinned search_path, EXECUTE revoked from PUBLIC and granted to
`anchor_lens` only, and a `lens_read` row whose id is taken first from
the table's sequence, so a rolled-back read still leaves its gap.
`anchor_lens` still selects no table: not `lens_round` either.

Reversible. Downgrade drops the function, the views, the two columns
and the table.

Revision ID: c6d2e8a4f917
Revises: e4c7a2d9b1f3
Create Date: 2026-09-29 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c6d2e8a4f917'
down_revision: Union[str, Sequence[str], None] = 'e4c7a2d9b1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'
LENS_ROLE = 'anchor_lens'

# Explicit column lists, never `*` (9e4b2c7a1f05's rule).
DEBUG_VIEWS: dict[str, str] = {
    'lens_round': (
        'id, consumer, weekly_review_id, lens_version_id, selected_note_ids, outcome, created_at'
    ),
    'review_proposal': (
        'id, review_id, kind, status, created_at, decided_at, lens_round_id, lens_note_ids, '
        'char_length(text) as text_len'
    ),
}

ROUNDS_FN = """
CREATE FUNCTION lens.rounds(n integer)
RETURNS TABLE (
    id integer, consumer text, outcome text, created_at timestamptz, titles text[]
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fn$
#variable_conflict use_column
DECLARE
    got integer;
    call_id bigint;
BEGIN
    -- First, and outside the transaction's reach: a rollback that
    -- undoes the row below still leaves this id used (e4c7a2d9b1f3).
    call_id := nextval(pg_get_serial_sequence('public.lens_read', 'id'));
    RETURN QUERY
        SELECT r.id, r.consumer::text, r.outcome::text, r.created_at,
               ARRAY(
                   SELECT ln.title::text
                   FROM unnest(r.selected_note_ids) WITH ORDINALITY AS u(note_id, ord)
                   JOIN public.lens_note AS ln ON ln.id = u.note_id
                   ORDER BY u.ord
               )
        FROM public.lens_round AS r
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT least(greatest(coalesce(n, 0), 0), 50);
    GET DIAGNOSTICS got = ROW_COUNT;
    INSERT INTO public.lens_read (id, fn, rows) VALUES (call_id, 'rounds', got);
    RETURN;
END
$fn$
"""

FUNCTION = 'lens.rounds(integer)'


def _grant_debug(views: Sequence[str]) -> None:
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
        'lens_round',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('consumer', sa.String(), nullable=False),
        sa.Column('weekly_review_id', sa.BigInteger(), nullable=True),
        sa.Column('lens_version_id', sa.Integer(), nullable=True),
        sa.Column(
            'selected_note_ids',
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column('rationale', sa.String(), nullable=True),
        sa.Column('outcome', sa.String(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.CheckConstraint("consumer in ('review')", name='ck_lens_round_consumer'),
        sa.CheckConstraint(
            "outcome in ('grounded', 'empty', 'fallback')", name='ck_lens_round_outcome'
        ),
        sa.ForeignKeyConstraint(['weekly_review_id'], ['weekly_review.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['lens_version_id'], ['lens_version.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_lens_round_weekly_review_id', 'lens_round', ['weekly_review_id'])

    op.add_column('review_proposal', sa.Column('lens_round_id', sa.Integer(), nullable=True))
    op.add_column(
        'review_proposal',
        sa.Column('lens_note_ids', postgresql.ARRAY(sa.Integer()), nullable=True),
    )
    op.create_foreign_key(
        'fk_review_proposal_lens_round_id',
        'review_proposal',
        'lens_round',
        ['lens_round_id'],
        ['id'],
        ondelete='SET NULL',
    )

    for name, columns in DEBUG_VIEWS.items():
        op.execute(f'CREATE VIEW debug.{name} AS SELECT {columns} FROM public.{name}')
    _grant_debug(list(DEBUG_VIEWS))

    op.execute(ROUNDS_FN)
    op.execute(f'REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC')
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LENS_ROLE}') THEN
                GRANT EXECUTE ON FUNCTION {FUNCTION} TO {LENS_ROLE};
            END IF;
        END
        $$
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(f'DROP FUNCTION IF EXISTS {FUNCTION}')
    for name in DEBUG_VIEWS:
        op.execute(f'DROP VIEW IF EXISTS debug.{name}')
    op.drop_constraint('fk_review_proposal_lens_round_id', 'review_proposal', type_='foreignkey')
    op.drop_column('review_proposal', 'lens_note_ids')
    op.drop_column('review_proposal', 'lens_round_id')
    op.drop_index('ix_lens_round_weekly_review_id', table_name='lens_round')
    op.drop_table('lens_round')
