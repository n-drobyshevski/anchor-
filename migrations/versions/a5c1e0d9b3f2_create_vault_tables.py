"""create the vault tables and user_state.vault_epoch

Milestone 8a (phase-8 plan section 6). Four tables, all empty until 8b
and 8c fill them, and one column:

- `vault_hold`: a vault change waiting for a yes in Telegram;
- `vault_file`: one tracked file (fact, journal day or opted-in note);
- `vault_chunk`: searchable pieces of opted-in notes (8d), with a
  generated Russian tsvector and a GIN index;
- `vault_status`: a singleton of operational timestamps;
- `user_state.vault_epoch`: six base32 characters, set here for the
  existing row and replaced by /delete.

Every invariant the plan states in a comment is a CHECK: the role/column
shape of `vault_file`, `held` iff there is a hold, a reason that is a
code rather than text, a path that is vault-relative, a hold payload
that is an object of the right shape, and the epoch's format.

The epoch is drawn here with `secrets`, not imported from app code, so
this revision means the same thing whatever app/ looks like later.

Reversible. The downgrade drops everything the vault tables recorded,
which the vault itself does not need: from 8b the database is the
source the files are rendered from, not the other way round.

Revision ID: a5c1e0d9b3f2
Revises: fa77c0b747fd
Create Date: 2026-09-25 12:00:00.000000

"""
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a5c1e0d9b3f2'
down_revision: Union[str, Sequence[str], None] = 'fa77c0b747fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EPOCH_ALPHABET = 'abcdefghijklmnopqrstuvwxyz234567'


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'vault_hold',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('status', sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('tg_message_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind in ('mass_delete', 'rule')", name='ck_vault_hold_kind'),
        sa.CheckConstraint(
            "status in ('pending', 'confirmed', 'reverted', 'expired', 'stale')",
            name='ck_vault_hold_status',
        ),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name='ck_vault_hold_payload_object'),
        sa.CheckConstraint(
            "kind <> 'mass_delete' or coalesce(jsonb_typeof(payload -> 'file_ids'), '') = 'array'",
            name='ck_vault_hold_mass_delete_payload',
        ),
        sa.CheckConstraint(
            "kind <> 'rule' or coalesce(payload ?& array['file_id', 'kind', 'text', 'supersedes_id'], false)",
            name='ck_vault_hold_rule_payload',
        ),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'vault_file',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('memory_id', sa.BigInteger(), nullable=True),
        sa.Column('local_date', sa.Date(), nullable=True),
        sa.Column('state', sa.String(), server_default=sa.text("'ok'"), nullable=False),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('hold_id', sa.BigInteger(), nullable=True),
        sa.Column('disk_sha256', sa.String(), nullable=True),
        sa.Column('render_digest', sa.String(), nullable=True),
        sa.Column('missing_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("role in ('fact', 'journal', 'note')", name='ck_vault_file_role'),
        sa.CheckConstraint(
            "state in ('ok', 'quarantined', 'held', 'restore', 'diverged', 'dismissed')",
            name='ck_vault_file_state',
        ),
        sa.CheckConstraint(
            "(role = 'fact' and local_date is null)"
            " or (role = 'journal' and memory_id is null and local_date is not null)"
            " or (role = 'note' and memory_id is null and local_date is null)",
            name='ck_vault_file_role_columns',
        ),
        sa.CheckConstraint("(state = 'held') = (hold_id is not null)", name='ck_vault_file_held_has_hold'),
        sa.CheckConstraint(
            "reason is null or reason ~ '^[a-z][a-z_]{0,39}$'", name='ck_vault_file_reason_code'
        ),
        sa.CheckConstraint(
            "path <> '' and left(path, 1) <> '/' and position(chr(92) in path) = 0",
            name='ck_vault_file_path_relative',
        ),
        sa.ForeignKeyConstraint(['memory_id'], ['memory.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['hold_id'], ['vault_hold.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('path'),
    )
    op.create_index('ix_vault_file_memory_id', 'vault_file', ['memory_id'], unique=False)

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

    op.create_table(
        'vault_status',
        sa.Column('id', sa.Integer(), server_default=sa.text('1'), autoincrement=False, nullable=False),
        sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_unavailable_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ob_running_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'forgets_window',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint('id = 1', name='ck_vault_status_singleton'),
        sa.CheckConstraint("jsonb_typeof(forgets_window) = 'array'", name='ck_vault_status_forgets_array'),
        sa.PrimaryKeyConstraint('id'),
    )

    # Nullable first, backfilled, then NOT NULL: the column has no
    # server default (see app/db/models.py for why).
    op.add_column('user_state', sa.Column('vault_epoch', sa.String(), nullable=True))
    epoch = ''.join(secrets.choice(_EPOCH_ALPHABET) for _ in range(6))
    op.get_bind().execute(sa.text('UPDATE user_state SET vault_epoch = :epoch'), {'epoch': epoch})
    op.alter_column('user_state', 'vault_epoch', nullable=False)
    op.create_check_constraint(
        'ck_user_state_vault_epoch', 'user_state', "vault_epoch ~ '^[a-z2-7]{6}$'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_user_state_vault_epoch', 'user_state', type_='check')
    op.drop_column('user_state', 'vault_epoch')
    op.drop_table('vault_status')
    op.drop_index('ix_vault_chunk_tsv', table_name='vault_chunk', postgresql_using='gin')
    op.drop_table('vault_chunk')
    op.drop_index('ix_vault_file_memory_id', table_name='vault_file')
    op.drop_table('vault_file')
    op.drop_table('vault_hold')
