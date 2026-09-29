"""the lens garden (L3): lens_garden_run, lens_gap, lens_note.aliases,
the `report` vault file, the `lens_garden` idle kind, and `lens.gaps()`

anchor-lens-plan.md sections 5, 8 and 11, milestone L3. Once a week an
idle job (`lens_garden`, app/core/idle/lens_garden.py) looks for gaps in
how the lens is organised -- links that should exist, notes that are
missing, tensions and bridges -- and proposes them; the user marks them
done or not needed in Telegram, and the next run checks the graph
again. Everything it keeps is here:

- `lens_note.aliases`: the note's frontmatter aliases, from vaultd's
  graph (the garden matches mentions by title *or* alias). Tags are not
  stored. Not part of the lens's version hash.
- `lens_garden_run`: one row per run, one per local ISO week (UNIQUE:
  the gate's "not twice in a week" is also the database's). The idle run
  it came from is SET NULL, not CASCADE: gaps must outlive idle pruning,
  and `idle_run.summary` may not hold text, so the run's findings (ids,
  scores, clusters with their model names, lens-sourced unresolved
  text) live here. `sent_at`, `tg_message_id`, `sent_gap_ids` and
  `sent_reopened` record the run's one Telegram message (owner amendment
  (b) to the L3 spec: one message per run, one keyboard row per open
  gap), so a tap can re-render that message with stable numbering and
  as it was sent: `sent_reopened` is each listed gap's `reopened` count
  then, so a later run reopening one of them does not rewrite this
  message's counts or add «снова» to it.
- `lens_gap`: one row per proposed gap. `garden_run_id` is the run
  that last raised or reopened it (CASCADE). `signature` is a sha256
  over the gap's kind and its normalised titles; the partial unique
  index `ux_lens_gap_signature_live` (where status <> 'resolved') makes
  a signature that is open, done, dismissed or researched impossible to
  raise twice, while a resolved one may recur. `tg_message_id` is the
  message that carries the gap's buttons.
- `vault_file.role` gains `report` (Anchor/Reports/Lens garden
  <week>-<epoch>.md): no memory, no date, no class.
- `idle_run.kind` gains `lens_garden` (`lens_research` waits for L4).

**Debug views** (granted to `anchor_debug`, as b8d24f6e0a17 explains):
`debug.lens_gap` has ids, kind, note ids, status, reopened, creation
and decision times, whether the gap was sent, and the detail's length.
Never its titles, title, detail, recheck payload or signature (a hash
of a few short titles can be guessed). Its status maps `resolved` and
`researched` to `closed`, as `lens.gaps()` does (below), and it has no
`resolved_at` for the same reason. `debug.lens_garden_run` has every
column but `findings`. `debug.lens_note` gains `alias_count`.

**Claude Code's door gains `lens.gaps(n)`** (plan section 11): the last
`least(n, 50)` gaps, newest first, with the run's week, kind, status,
titles, proposed title, detail, reopened count and times. Its text is
safe to show where `lens_round.rationale` is not: the model that wrote
it saw lens notes only (titles, summaries or, as in the L2 catalog,
the start of the text where a note has none, lens links, findings;
never a whole body, knowledge titles, dialogs or memory -- spec section
6), which is what `lens.notes()` and `lens.graph()` already expose, plus
each note's count of knowledge neighbours (a number, never a title). **Status is
reported as `open`, `done`, `dismissed` or `closed`** (owner amendment
(a)): `resolved` -- and L4's `researched` -- read as `closed`, so that
a resolved `missing_note` does not tell Claude Code that a note with
that title now exists, which may be a knowledge note it may not see.
The word alone would still say it (the gap's sources still in the lens,
no lens note by that title: only a knowledge note or a lens alias is
left), so a closed `missing_note` also comes back with no title and no
detail. The proposed title of an *open* one is still returned: an
earlier read can be joined by id, which the owner accepted (decisions.md,
L3). `titles` are the notes' current titles, as `lens.rounds()` does (a
note no longer in the lens is skipped), and a gap any of whose notes
has left the lens -- moved into a knowledge folder, say -- comes back
with no title and no detail, since the model's text may name that
note. `resolved_at` is not returned either. SECURITY DEFINER exactly as
e4c7a2d9b1f3's functions: a pinned search_path, EXECUTE revoked from
PUBLIC and granted to `anchor_lens` only, and a `lens_read` row whose
id is taken first from the table's sequence, so a rolled-back read
still leaves its gap. `anchor_lens` still selects no table.

Reversible. Downgrade deletes `lens_garden` idle runs and `report` file
rows (the constraints they need go with it), then drops the function,
the views, the tables and the column, and restores `debug.lens_note`.

Revision ID: b3e9f5a1c7d2
Revises: c6d2e8a4f917
Create Date: 2026-09-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b3e9f5a1c7d2'
down_revision: Union[str, Sequence[str], None] = 'c6d2e8a4f917'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'
LENS_ROLE = 'anchor_lens'

# `resolved` and L4's `researched` are shown as `closed` wherever Claude
# Code can look (owner amendment (a); module docstring). The word alone
# is not enough for a `missing_note`: its sources still in the lens and
# no lens note by that title, `closed` would still mean "a knowledge
# note (or a lens alias) with this title now exists". So lens.gaps()
# also returns no title and no detail for a closed `missing_note`
# (GAPS_FN).
PUBLIC_STATUS = (
    "case when {col} in ('resolved', 'researched') then 'closed' else {col} end"
)

# Explicit column lists, never `*` (9e4b2c7a1f05's rule).
DEBUG_VIEWS: dict[str, str] = {
    'lens_gap': (
        'id, garden_run_id, kind, note_ids, '
        + PUBLIC_STATUS.format(col='status')
        + ' as status, reopened, created_at, decided_at, '
        'tg_message_id is not null as sent, char_length(detail) as detail_len'
    ),
    'lens_garden_run': (
        'id, idle_run_id, iso_week, lens_version_id, created_at, sent_at, tg_message_id, '
        'sent_gap_ids, sent_reopened'
    ),
}
LENS_NOTE_VIEW_BEFORE = 'id, vault_file_id, kind, body_hash, chars, updated_at'
LENS_NOTE_VIEW_AFTER = LENS_NOTE_VIEW_BEFORE + ', cardinality(aliases) as alias_count'

IDLE_KINDS_BEFORE = (
    "'backfill', 'consolidate', 'reflect', 'prebrief', 'critique', 'research', 'canary'"
)
IDLE_KINDS_AFTER = (
    "'backfill', 'consolidate', 'reflect', 'prebrief', 'critique', 'lens_garden', "
    "'research', 'canary'"
)

ROLES_BEFORE = "role in ('fact', 'journal', 'note')"
ROLES_AFTER = "role in ('fact', 'journal', 'note', 'report')"
ROLE_COLUMNS_BEFORE = (
    "(role = 'fact' and local_date is null and note_class is null)"
    " or (role = 'journal' and memory_id is null and local_date is not null"
    " and note_class is null)"
    " or (role = 'note' and memory_id is null and local_date is null"
    " and note_class is not null)"
)
ROLE_COLUMNS_AFTER = (
    ROLE_COLUMNS_BEFORE
    + " or (role = 'report' and memory_id is null and local_date is null"
    " and note_class is null)"
)

GAPS_FN = """
CREATE FUNCTION lens.gaps(n integer)
RETURNS TABLE (
    id integer, week text, kind text, status text, titles text[], title text, detail text,
    reopened integer, created_at timestamptz, decided_at timestamptz
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
        SELECT g.id, r.iso_week::text, g.kind::text,
               CASE WHEN g.status IN ('resolved', 'researched') THEN 'closed'
                    ELSE g.status::text END,
               -- Current titles, as lens.rounds does: a note no longer
               -- in the lens is skipped.
               ARRAY(
                   SELECT ln.title::text
                   FROM unnest(g.note_ids) WITH ORDINALITY AS u(note_id, ord)
                   JOIN public.lens_note AS ln ON ln.id = u.note_id
                   ORDER BY u.ord
               ),
               CASE WHEN v.shown THEN g.title::text END,
               CASE WHEN v.shown THEN g.detail::text END,
               g.reopened, g.created_at, g.decided_at
        FROM public.lens_gap AS g
        JOIN public.lens_garden_run AS r ON r.id = g.garden_run_id
        CROSS JOIN LATERAL (
            SELECT NOT (g.kind = 'missing_note' AND g.status IN ('resolved', 'researched'))
                   AND NOT EXISTS (
                       SELECT 1 FROM unnest(g.note_ids) AS u(note_id)
                       WHERE NOT EXISTS (
                           SELECT 1 FROM public.lens_note AS ln WHERE ln.id = u.note_id
                       )
                   ) AS shown
        ) AS v
        ORDER BY g.created_at DESC, g.id DESC
        LIMIT least(greatest(coalesce(n, 0), 0), 50);
    GET DIAGNOSTICS got = ROW_COUNT;
    INSERT INTO public.lens_read (id, fn, rows) VALUES (call_id, 'gaps', got);
    RETURN;
END
$fn$
"""

FUNCTION = 'lens.gaps(integer)'


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


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_='check')
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'lens_note',
        sa.Column(
            'aliases', postgresql.ARRAY(sa.String()), server_default=sa.text("'{}'"), nullable=False
        ),
    )

    op.create_table(
        'lens_garden_run',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('idle_run_id', sa.BigInteger(), nullable=True),
        sa.Column('iso_week', sa.String(), nullable=False),
        sa.Column('lens_version_id', sa.Integer(), nullable=True),
        sa.Column(
            'findings',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('tg_message_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'sent_gap_ids',
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            'sent_reopened',
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "iso_week ~ '^[0-9]{4}-W[0-9]{2}$'", name='ck_lens_garden_run_iso_week'
        ),
        sa.CheckConstraint(
            'tg_message_id is null or sent_at is not null', name='ck_lens_garden_run_sent'
        ),
        sa.ForeignKeyConstraint(['idle_run_id'], ['idle_run.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['lens_version_id'], ['lens_version.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('iso_week', name='uq_lens_garden_run_iso_week'),
    )
    op.create_table(
        'lens_gap',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('garden_run_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column(
            'note_ids', postgresql.ARRAY(sa.Integer()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            'titles', postgresql.ARRAY(sa.String()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('detail', sa.String(), nullable=False),
        sa.Column('signature', sa.String(), nullable=False),
        sa.Column(
            'recheck',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column('status', sa.String(), server_default=sa.text("'open'"), nullable=False),
        sa.Column('reopened', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('tg_message_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind in ('link', 'missing_note', 'tension', 'bridge')", name='ck_lens_gap_kind'
        ),
        sa.CheckConstraint(
            "status in ('open', 'done', 'dismissed', 'resolved', 'researched')",
            name='ck_lens_gap_status',
        ),
        sa.CheckConstraint('char_length(title) <= 80', name='ck_lens_gap_title_len'),
        sa.CheckConstraint('char_length(detail) <= 300', name='ck_lens_gap_detail_len'),
        sa.CheckConstraint("signature ~ '^[0-9a-f]{64}$'", name='ck_lens_gap_signature'),
        sa.CheckConstraint('reopened >= 0', name='ck_lens_gap_reopened'),
        sa.CheckConstraint(
            "(status = 'resolved') = (resolved_at is not null)", name='ck_lens_gap_resolved_at'
        ),
        sa.ForeignKeyConstraint(['garden_run_id'], ['lens_garden_run.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_lens_gap_garden_run_id', 'lens_gap', ['garden_run_id'])
    op.create_index(
        'ux_lens_gap_signature_live',
        'lens_gap',
        ['signature'],
        unique=True,
        postgresql_where=sa.text("status <> 'resolved'"),
    )

    _replace_check('vault_file', 'ck_vault_file_role', ROLES_AFTER)
    _replace_check('vault_file', 'ck_vault_file_role_columns', ROLE_COLUMNS_AFTER)
    _replace_check('idle_run', 'ck_idle_run_kind', f'kind in ({IDLE_KINDS_AFTER})')

    for name, columns in DEBUG_VIEWS.items():
        op.execute(f'CREATE VIEW debug.{name} AS SELECT {columns} FROM public.{name}')
    # A new column at the end: CREATE OR REPLACE keeps the grant.
    op.execute(
        f'CREATE OR REPLACE VIEW debug.lens_note AS SELECT {LENS_NOTE_VIEW_AFTER} '
        'FROM public.lens_note'
    )
    _grant_debug(list(DEBUG_VIEWS))

    op.execute(GAPS_FN)
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
    # A column cannot leave a view through CREATE OR REPLACE.
    op.execute('DROP VIEW IF EXISTS debug.lens_note')
    op.execute(
        f'CREATE VIEW debug.lens_note AS SELECT {LENS_NOTE_VIEW_BEFORE} FROM public.lens_note'
    )
    _grant_debug(['lens_note'])

    op.execute("DELETE FROM idle_run WHERE kind = 'lens_garden'")
    _replace_check('idle_run', 'ck_idle_run_kind', f'kind in ({IDLE_KINDS_BEFORE})')
    op.execute("DELETE FROM vault_file WHERE role = 'report'")
    _replace_check('vault_file', 'ck_vault_file_role_columns', ROLE_COLUMNS_BEFORE)
    _replace_check('vault_file', 'ck_vault_file_role', ROLES_BEFORE)

    op.drop_index('ux_lens_gap_signature_live', table_name='lens_gap')
    op.drop_index('ix_lens_gap_garden_run_id', table_name='lens_gap')
    op.drop_table('lens_gap')
    op.drop_table('lens_garden_run')
    op.drop_column('lens_note', 'aliases')
