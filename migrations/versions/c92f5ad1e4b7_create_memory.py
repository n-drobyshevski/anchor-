"""create memory and pending_memory, enable pg_trgm

Milestone 2b (phase-2 plan sections 4 and 11). A new revision on top of
2a's head; earlier revisions are never edited.

**Reversibility, honestly.** 2a's migration could call its downgrade
harmless because scenes and jobs are derived data. This one is not:
downgrade destroys user-authored memories, which are derivable from
nothing. It is reversible in the schema sense only.

`pg_trgm` is created but deliberately **not** dropped on downgrade. It
is a database-scoped object, not a table-scoped one, and this migration
stops owning it the moment anything else could depend on it.

Revision ID: c92f5ad1e4b7
Revises: b7c14e9f2a30
Create Date: 2026-09-22 09:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c92f5ad1e4b7'
down_revision: Union[str, Sequence[str], None] = 'b7c14e9f2a30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# A word with an ASCII-free stem, so the assertion cannot pass on
# ASCII handling alone. Under C.UTF-8 or en_US.utf8 this yields 11
# trigrams (7 for "привет" with padding, 4 for "мир").
_TRGM_PROBE = "привет мир"
_TRGM_MIN_TRIGRAMS = 8

_TRGM_FAILURE = """
pg_trgm cannot see Cyrillic in this database.

    SELECT show_trgm('{probe}')  ->  {count} trigrams (expected >= {expected})

This is a database *locale* problem, not a pg_trgm version problem: under
a plain 'C'/'POSIX' locale, pg_trgm treats non-ASCII bytes as
non-alphanumeric, so show_trgm() returns nothing and every similarity()
and word_similarity() over Russian text returns 0 -- silently, with no
error. Memory retrieval would return nothing, forever, and look like a
bug in the retrieval code rather than a misconfigured database.

A database's locale is fixed at CREATE DATABASE and cannot be altered in
place. Create the database with an explicit UTF-8 locale, e.g.

    CREATE DATABASE anchor TEMPLATE template0 LOCALE 'C.UTF-8' ENCODING 'UTF8';

and migrate into that. If that is impossible on your platform, the
documented fallback (phase-2 plan section 4) is to replace trigram
retrieval with full-text search: to_tsvector('russian', text) plus a GIN
index.
""".strip()


def _assert_trgm_sees_cyrillic() -> None:
    """Abort the migration if pg_trgm is blind to Cyrillic here.

    The plan (section 4) asks for this check "in the migration test".
    It runs here as well, and deliberately hard-fails: the failure mode
    it guards against is silent -- zero rows retrieved, no error, no log
    line -- so it must be impossible for a database in that state to
    carry the schema that depends on it.

    The margin is wide (11 vs 0), so a false abort is implausible.
    """
    connection = op.get_bind()
    count = connection.execute(
        sa.text("SELECT coalesce(array_length(show_trgm(:probe), 1), 0)"),
        {"probe": _TRGM_PROBE},
    ).scalar_one()
    if count < _TRGM_MIN_TRIGRAMS:
        raise RuntimeError(
            _TRGM_FAILURE.format(probe=_TRGM_PROBE, count=count, expected=_TRGM_MIN_TRIGRAMS)
        )


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    _assert_trgm_sees_cyrillic()

    op.create_table(
        'memory',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('pinned', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('superseded_by', sa.BigInteger(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('use_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "kind in ('identity', 'preference', 'event', 'rule', 'technique')",
            name='ck_memory_kind',
        ),
        sa.CheckConstraint('char_length("text") <= 300', name='ck_memory_text_length'),
        sa.CheckConstraint('superseded_by <> id', name='ck_memory_no_self_supersede'),
        sa.ForeignKeyConstraint(['superseded_by'], ['memory.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'memory_trgm', 'memory', ['text'],
        postgresql_using='gin', postgresql_ops={'text': 'gin_trgm_ops'},
    )

    op.create_table(
        'pending_memory',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('char_length("text") <= 300', name='ck_pending_memory_text_length'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema. Destroys user-authored memories -- see the docstring."""
    op.drop_table('pending_memory')
    op.drop_index('memory_trgm', table_name='memory')
    op.drop_table('memory')
    # pg_trgm is deliberately left in place; see the module docstring.
