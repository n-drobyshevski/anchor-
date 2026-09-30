"""the lens (L1): lens_note, note_link, lens_version, lens_read, and
the anchor_lens role with its `lens` schema

anchor-lens-plan.md sections 5 and 11, milestone L1. Four tables:

- `lens_note`: every lens note, whole (title, kind, summary, body, a
  hash and a length), one row per `vault_file`, cascading with it. The
  sync pass writes it only while notes consent, VAULT_KNOWLEDGE_ENABLED
  and LENS_ENABLED are all on (app/vault/lens.py).
- `note_link`: the wikilinks out of knowledge and lens notes, from
  vaultd's graph. A CHECK allows exactly one target: another note's
  row, the unresolved target text, or `outside` -- a note that exists
  but is not the bot's to see, which carries nothing that names it.
- `lens_version`: one row per distinct hash of the lens.
- `lens_read`: one row per call of a `lens` function below.

**Claude Code's door: the `anchor_lens` role** (plan section 11).
Created NOLOGIN, exactly as 9e4b2c7a1f05 creates `anchor_debug`
(skipped with a NOTICE when the migrating user may not create roles;
the password and LOGIN are a manual step, or `/lens code on`). It gets
CONNECT, USAGE on schema `lens` and EXECUTE on two functions, and
nothing else: no `public` table, no `debug` view.

- `lens.notes()`: id, kind, title, summary, body, chars, updated_at.
- `lens.graph()`: lens-to-lens edges by title, and the unresolved
  targets of links out of lens notes. Never a knowledge-only note, never
  an outside link.

Both are SECURITY DEFINER with a pinned search_path, owned by the
migrating user, EXECUTE revoked from PUBLIC, and each inserts one
`lens_read` row (its name and its row count) before it returns.

**A rollback cannot hide a read.** That row lives in the caller's
transaction, so `begin; select * from lens.notes(); rollback` would get
every body and take the row back with it. So each call first takes its
row's id from `lens_read`'s own id sequence, and a sequence never rolls
back: a read whose row was undone leaves a gap in `lens_read.id`. /lens
(and the daily digest, for gaps between two recorded reads) reports the
gaps as reads without a record (app/vault/lens.py's `unrecorded_*`).
/delete's TRUNCATE ... RESTART IDENTITY resets the sequence with the
table, so the ids stay dense. One false gap is possible: after a
Postgres crash, a sequence can skip ahead (up to 32 values); the /lens
line names that as the other cause.

**Debug views** (granted to `anchor_debug` here, as b8d24f6e0a17
explains): `debug.lens_note` without title, summary or body;
`debug.note_link` with `unresolved` as a boolean, never the text;
`debug.lens_version` and `debug.lens_read` whole (hashes, counts,
times, function names).

Reversible. Downgrade drops the schema, the views and the tables; the
role is dropped only when nothing else still depends on it.

Revision ID: e4c7a2d9b1f3
Revises: 8b4f1d3a9e26
Create Date: 2026-09-29 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e4c7a2d9b1f3'
down_revision: Union[str, Sequence[str], None] = '8b4f1d3a9e26'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'
LENS_ROLE = 'anchor_lens'

# Explicit column lists, never `*` (9e4b2c7a1f05's rule).
LENS_DEBUG_VIEWS: dict[str, str] = {
    'lens_note': 'id, vault_file_id, kind, body_hash, chars, updated_at',
    'note_link': (
        'id, src_file_id, dst_file_id, outside, unresolved_text is not null as unresolved'
    ),
    'lens_version': 'id, hash, note_count, created_at',
    'lens_read': 'id, at, fn, rows',
}

NOTES_FN = """
CREATE FUNCTION lens.notes()
RETURNS TABLE (
    id integer, kind text, title text, summary text, body text, chars integer,
    updated_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fn$
#variable_conflict use_column
DECLARE
    n integer;
    call_id bigint;
BEGIN
    -- First, and outside the transaction's reach: a rollback that
    -- undoes the row below still leaves this id used (module docstring).
    call_id := nextval(pg_get_serial_sequence('public.lens_read', 'id'));
    RETURN QUERY
        SELECT ln.id, ln.kind::text, ln.title::text, ln.summary::text, ln.body::text,
               ln.chars, ln.updated_at
        FROM public.lens_note AS ln
        ORDER BY ln.title, ln.id;
    GET DIAGNOSTICS n = ROW_COUNT;
    INSERT INTO public.lens_read (id, fn, rows) VALUES (call_id, 'notes', n);
    RETURN;
END
$fn$
"""

GRAPH_FN = """
CREATE FUNCTION lens.graph()
RETURNS TABLE (src_title text, dst_title text, unresolved text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fn$
#variable_conflict use_column
DECLARE
    n integer;
    call_id bigint;
BEGIN
    -- First, and outside the transaction's reach: a rollback that
    -- undoes the row below still leaves this id used (module docstring).
    call_id := nextval(pg_get_serial_sequence('public.lens_read', 'id'));
    RETURN QUERY
        SELECT e.src_title, e.dst_title, e.unresolved
        FROM (
            SELECT s.title::text AS src_title, d.title::text AS dst_title,
                   NULL::text AS unresolved
            FROM public.note_link AS l
            JOIN public.lens_note AS s ON s.vault_file_id = l.src_file_id
            JOIN public.lens_note AS d ON d.vault_file_id = l.dst_file_id
            UNION ALL
            SELECT s.title::text, NULL::text, l.unresolved_text::text
            FROM public.note_link AS l
            JOIN public.lens_note AS s ON s.vault_file_id = l.src_file_id
            WHERE l.unresolved_text IS NOT NULL
        ) AS e
        ORDER BY e.src_title, e.dst_title NULLS LAST, e.unresolved NULLS LAST;
    GET DIAGNOSTICS n = ROW_COUNT;
    INSERT INTO public.lens_read (id, fn, rows) VALUES (call_id, 'graph', n);
    RETURN;
END
$fn$
"""

FUNCTIONS = ('lens.notes()', 'lens.graph()')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'lens_note',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('vault_file_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('summary', sa.String(), nullable=True),
        sa.Column('body', sa.String(), nullable=False),
        sa.Column('body_hash', sa.String(), nullable=False),
        sa.Column('chars', sa.Integer(), nullable=False),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.CheckConstraint("kind in ('person', 'concept')", name='ck_lens_note_kind'),
        sa.CheckConstraint('chars >= 0', name='ck_lens_note_chars'),
        sa.ForeignKeyConstraint(['vault_file_id'], ['vault_file.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vault_file_id'),
    )
    op.create_table(
        'note_link',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('src_file_id', sa.BigInteger(), nullable=False),
        sa.Column('dst_file_id', sa.BigInteger(), nullable=True),
        sa.Column('unresolved_text', sa.String(), nullable=True),
        sa.Column('outside', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.CheckConstraint(
            '(dst_file_id is not null)::int + (unresolved_text is not null)::int'
            ' + outside::int = 1',
            name='ck_note_link_one_target',
        ),
        sa.ForeignKeyConstraint(['src_file_id'], ['vault_file.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['dst_file_id'], ['vault_file.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_note_link_src_file_id', 'note_link', ['src_file_id'])
    op.create_index('ix_note_link_dst_file_id', 'note_link', ['dst_file_id'])
    op.create_table(
        'lens_version',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('hash', sa.String(), nullable=False),
        sa.Column('note_count', sa.Integer(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('hash'),
    )
    op.create_table(
        'lens_read',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('fn', sa.String(), nullable=False),
        sa.Column('rows', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_lens_read_at', 'lens_read', ['at'])

    for name, columns in LENS_DEBUG_VIEWS.items():
        op.execute(f'CREATE VIEW debug.{name} AS SELECT {columns} FROM public.{name}')
    grants = '\n'.join(
        f'GRANT SELECT ON debug.{name} TO {DEBUG_ROLE};' for name in LENS_DEBUG_VIEWS
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

    op.execute('CREATE SCHEMA lens')
    op.execute('REVOKE ALL ON SCHEMA lens FROM PUBLIC')
    op.execute(NOTES_FN)
    op.execute(GRAPH_FN)
    for fn in FUNCTIONS:
        op.execute(f'REVOKE ALL ON FUNCTION {fn} FROM PUBLIC')

    execute_grants = '\n'.join(
        f'GRANT EXECUTE ON FUNCTION {fn} TO {LENS_ROLE};' for fn in FUNCTIONS
    )
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LENS_ROLE}') THEN
                IF (SELECT rolsuper OR rolcreaterole FROM pg_roles
                    WHERE rolname = current_user) THEN
                    CREATE ROLE {LENS_ROLE} NOLOGIN;
                ELSE
                    RAISE NOTICE 'cannot create role {LENS_ROLE}; create it by hand';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LENS_ROLE}') THEN
                EXECUTE format('GRANT CONNECT ON DATABASE %I TO {LENS_ROLE}',
                               current_database());
                GRANT USAGE ON SCHEMA lens TO {LENS_ROLE};
                {execute_grants}
            END IF;
        END
        $$
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP SCHEMA lens CASCADE')
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{LENS_ROLE}') THEN
                EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {LENS_ROLE}',
                               current_database());
                BEGIN
                    DROP ROLE {LENS_ROLE};
                EXCEPTION WHEN dependent_objects_still_exist THEN
                    RAISE NOTICE 'role {LENS_ROLE} still used elsewhere; kept';
                END;
            END IF;
        END
        $$
    """)
    for name in LENS_DEBUG_VIEWS:
        op.execute(f'DROP VIEW IF EXISTS debug.{name}')
    op.drop_index('ix_lens_read_at', table_name='lens_read')
    op.drop_table('lens_read')
    op.drop_table('lens_version')
    op.drop_index('ix_note_link_dst_file_id', table_name='note_link')
    op.drop_index('ix_note_link_src_file_id', table_name='note_link')
    op.drop_table('note_link')
    op.drop_table('lens_note')
