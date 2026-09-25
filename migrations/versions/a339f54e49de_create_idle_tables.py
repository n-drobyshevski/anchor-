"""create idle_run, idle_change, brief_note, interest_topic, backup_log,
heartbeat_state

Milestone 6a (Phase 6 plan section 3; approved plan §5, §6, §7). The
five idle-framework tables, plus `heartbeat_state`, a single-row
liveness marker the heartbeat loop writes every 60s and 6e's `/readyz`
will read.

`idle_change.after` is 6a's own addition to the plan's SQL (approved
plan §6, "answers received": "idle_change.after jsonb approved") --
undo.py's conflict check needs the row's state right after the change,
not only its state before.

`state_change.source` has no CHECK constraint in this schema (unlike
`memory.kind` or `study_job.status`, it is deliberately open, the same
way `spend_ledger.category` is -- see that column's own docstring), so
there is nothing to widen for the undo engine's `source='undo'` rows.

Reversible. Downgrade drops all six tables -- idle history and any
logged undo trail are not derivable from anything else.

Revision ID: a339f54e49de
Revises: c1a5e9f4b6d2
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a339f54e49de'
down_revision: Union[str, Sequence[str], None] = 'c1a5e9f4b6d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'idle_run',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('status', sa.String(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column('skip_reason', sa.String(), nullable=True),
        sa.Column('usd_cost', sa.Numeric(10, 6), server_default=sa.text('0'), nullable=False),
        sa.Column(
            'summary', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"), nullable=False,
        ),
        sa.Column('reversible', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('undone_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "kind in ('backfill', 'consolidate', 'reflect', 'prebrief', 'critique', "
            "'research', 'canary')",
            name='ck_idle_run_kind',
        ),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'done', 'failed', 'skipped', 'undone')",
            name='ck_idle_run_status',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_idle_run_local_date_kind', 'idle_run', ['local_date', 'kind'])

    op.create_table(
        'idle_change',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.BigInteger(), nullable=False),
        sa.Column('table_name', sa.String(), nullable=False),
        sa.Column('row_id', sa.BigInteger(), nullable=False),
        sa.Column('op', sa.String(), nullable=False),
        sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "table_name in ('memory', 'notebook_entry')", name='ck_idle_change_table_name'
        ),
        sa.CheckConstraint(
            "op in ('insert', 'supersede', 'close', 'update')", name='ck_idle_change_op'
        ),
        sa.ForeignKeyConstraint(['run_id'], ['idle_run.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_idle_change_run_id', 'idle_change', ['run_id'])

    op.create_table(
        'brief_note',
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('notes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('local_date'),
    )

    op.create_table(
        'interest_topic',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('packet', sa.String(), nullable=False),
        sa.Column('active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('char_length("text") <= 100', name='ck_interest_topic_text_length'),
        sa.CheckConstraint("packet in ('forums', 'guides', 'ref')", name='ck_interest_topic_packet'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'backup_log',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('object_key', sa.String(), nullable=True),
        sa.Column('bytes', sa.BigInteger(), nullable=True),
        sa.Column('sha256', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.CheckConstraint("status in ('ok', 'failed', 'pruned', 'purged')", name='ck_backup_log_status'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'heartbeat_state',
        sa.Column('id', sa.Integer(), server_default=sa.text('1'), autoincrement=False, nullable=False),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('id = 1', name='ck_heartbeat_state_id_singleton'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.execute(sa.text("insert into heartbeat_state (id, heartbeat_at) values (1, null)"))


def downgrade() -> None:
    """Downgrade schema. Drops idle history and any logged undo trail."""
    op.drop_table('heartbeat_state')
    op.drop_table('backup_log')
    op.drop_table('interest_topic')
    op.drop_table('brief_note')
    op.drop_index('ix_idle_change_run_id', table_name='idle_change')
    op.drop_table('idle_change')
    op.drop_index('ix_idle_run_local_date_kind', table_name='idle_run')
    op.drop_table('idle_run')
