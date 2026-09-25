"""split note chunks into personal and knowledge; add notes consent

Milestone 8e (8e plan section 5). A note Anchor can see is either
`personal` (about the user) or `knowledge` (generic), and the two are
stored apart, in tables the database will not let anyone mix up:

- `vault_chunk` goes. `note_chunk_personal` and `note_chunk_knowledge`
  replace it, with the same shape plus a constant `note_class` column
  (a CHECK pins each table's value) and a composite foreign key
  `(file_id, note_class) -> vault_file(id, note_class) ON DELETE
  CASCADE`. A personal chunk under a knowledge file is refused, and so
  is reclassifying a file while chunks of its old class exist: the code
  must delete them first (8d).
- `vault_file.note_class`: `personal` or `knowledge` for a note, and
  nothing for a fact or a journal day (`ck_vault_file_role_columns`),
  plus `UNIQUE (id, note_class)`, the target of those keys.
- `user_state.notes_consent`: false until `/vault notes on`.
- The debug views follow: `debug.vault_chunk` becomes one view per
  chunk table (ids, order and a length; no text, heading or path), and
  `debug.vault_file` gains `note_class`. Each is granted here, as
  b8d24f6e0a17 explains.

**It refuses to run over existing notes rather than guess their
class.** Nothing on `main` writes `vault_chunk` or a `role='note'` file
row before 8d, so both should be empty. If either is not, the upgrade
stops and names the cause; deleting the rows by hand is safe, since
they are derived from the vault and 8d rebuilds them
(docs/decisions.md, "8e -- the migration refuses").

Reversible. The downgrade drops both chunk tables (derived data) and
recreates an empty `vault_chunk`.

Revision ID: c3e8f5a1d2b6
Revises: b8d24f6e0a17
Create Date: 2026-09-25 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c3e8f5a1d2b6'
down_revision: Union[str, Sequence[str], None] = 'b8d24f6e0a17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'

CHUNK_TABLES = {'note_chunk_personal': 'personal', 'note_chunk_knowledge': 'knowledge'}

CHUNK_VIEW_COLUMNS = 'id, file_id, ord, char_length(text) as text_len'

OLD_VAULT_FILE_VIEW = (
    'id, role, state, reason, memory_id, local_date, hold_id, '
    'disk_sha256 is not null as seen, render_digest is not null as rendered, '
    'missing_since, created_at, updated_at'
)
OLD_VAULT_CHUNK_VIEW = (
    'id, file_id, ord, char_length(heading) as heading_len, char_length(text) as text_len'
)

OLD_ROLE_COLUMNS = (
    "(role = 'fact' and local_date is null)"
    " or (role = 'journal' and memory_id is null and local_date is not null)"
    " or (role = 'note' and memory_id is null and local_date is null)"
)
NEW_ROLE_COLUMNS = (
    "(role = 'fact' and local_date is null and note_class is null)"
    " or (role = 'journal' and memory_id is null and local_date is not null"
    " and note_class is null)"
    " or (role = 'note' and memory_id is null and local_date is null"
    " and note_class is not null)"
)


class NotesExist(RuntimeError):
    pass


def _refuse_existing_notes() -> None:
    bind = op.get_bind()
    chunks = bind.execute(sa.text('SELECT EXISTS (SELECT 1 FROM vault_chunk)')).scalar_one()
    if chunks:
        raise NotesExist(
            'vault_chunk has rows; 8e will not guess whether they are personal or knowledge. '
            'They are derived from the vault: DELETE FROM vault_chunk, then upgrade again.'
        )
    notes = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM vault_file WHERE role = 'note')")
    ).scalar_one()
    if notes:
        raise NotesExist(
            "vault_file has role='note' rows; 8e will not guess their class. They are derived "
            "from the vault: DELETE FROM vault_file WHERE role = 'note', then upgrade again."
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
    _refuse_existing_notes()

    op.execute('DROP VIEW debug.vault_chunk')
    op.drop_index('ix_vault_chunk_tsv', table_name='vault_chunk', postgresql_using='gin')
    op.drop_table('vault_chunk')

    op.add_column('vault_file', sa.Column('note_class', sa.String(), nullable=True))
    op.create_check_constraint(
        'ck_vault_file_note_class', 'vault_file', "note_class in ('personal', 'knowledge')"
    )
    op.drop_constraint('ck_vault_file_role_columns', 'vault_file', type_='check')
    op.create_check_constraint('ck_vault_file_role_columns', 'vault_file', NEW_ROLE_COLUMNS)
    op.create_unique_constraint(
        'uq_vault_file_id_note_class', 'vault_file', ['id', 'note_class']
    )

    for table, note_class in CHUNK_TABLES.items():
        op.create_table(
            table,
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('file_id', sa.BigInteger(), nullable=False),
            sa.Column(
                'note_class', sa.String(), server_default=sa.text(f"'{note_class}'"), nullable=False
            ),
            sa.Column('ord', sa.Integer(), nullable=False),
            sa.Column('heading', sa.String(), nullable=True),
            sa.Column('text', sa.String(), nullable=False),
            sa.Column(
                'tsv',
                postgresql.TSVECTOR(),
                sa.Computed(
                    "to_tsvector('russian', coalesce(heading, '') || ' ' || \"text\")",
                    persisted=True,
                ),
                nullable=True,
            ),
            sa.CheckConstraint(f"note_class = '{note_class}'", name=f'ck_{table}_class'),
            sa.CheckConstraint('char_length(heading) <= 200', name=f'ck_{table}_heading_length'),
            sa.CheckConstraint('char_length("text") <= 1200', name=f'ck_{table}_text_length'),
            sa.ForeignKeyConstraint(
                ['file_id', 'note_class'],
                ['vault_file.id', 'vault_file.note_class'],
                ondelete='CASCADE',
                name=f'fk_{table}_file_class',
            ),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('file_id', 'ord', name=f'uq_{table}_file_ord'),
        )
        op.create_index(f'ix_{table}_tsv', table, ['tsv'], unique=False, postgresql_using='gin')
        op.execute(f'CREATE VIEW debug.{table} AS SELECT {CHUNK_VIEW_COLUMNS} FROM public.{table}')

    # Appending a column is what CREATE OR REPLACE VIEW allows.
    op.execute(
        f'CREATE OR REPLACE VIEW debug.vault_file AS SELECT {OLD_VAULT_FILE_VIEW}, note_class '
        'FROM public.vault_file'
    )
    _grant(list(CHUNK_TABLES))

    op.add_column(
        'user_state',
        sa.Column('notes_consent', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user_state', 'notes_consent')

    op.execute('DROP VIEW debug.vault_file')
    op.execute(f'CREATE VIEW debug.vault_file AS SELECT {OLD_VAULT_FILE_VIEW} FROM public.vault_file')
    for table in CHUNK_TABLES:
        op.execute(f'DROP VIEW debug.{table}')
        op.drop_index(f'ix_{table}_tsv', table_name=table, postgresql_using='gin')
        op.drop_table(table)

    op.drop_constraint('uq_vault_file_id_note_class', 'vault_file', type_='unique')
    op.drop_constraint('ck_vault_file_role_columns', 'vault_file', type_='check')
    op.create_check_constraint('ck_vault_file_role_columns', 'vault_file', OLD_ROLE_COLUMNS)
    op.drop_constraint('ck_vault_file_note_class', 'vault_file', type_='check')
    op.drop_column('vault_file', 'note_class')

    op.create_table(
        'vault_chunk',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('file_id', sa.BigInteger(), nullable=False),
        sa.Column('ord', sa.Integer(), nullable=False),
        sa.Column('heading', sa.String(), nullable=True),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column(
            'tsv',
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('russian', coalesce(heading, '') || ' ' || \"text\")", persisted=True
            ),
            nullable=True,
        ),
        sa.CheckConstraint('char_length(heading) <= 200', name='ck_vault_chunk_heading_length'),
        sa.CheckConstraint('char_length("text") <= 1200', name='ck_vault_chunk_text_length'),
        sa.ForeignKeyConstraint(['file_id'], ['vault_file.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('file_id', 'ord', name='uq_vault_chunk_file_ord'),
    )
    op.create_index('ix_vault_chunk_tsv', 'vault_chunk', ['tsv'], unique=False, postgresql_using='gin')
    op.execute(f'CREATE VIEW debug.vault_chunk AS SELECT {OLD_VAULT_CHUNK_VIEW} FROM public.vault_chunk')
    _grant(['vault_chunk', 'vault_file'])
