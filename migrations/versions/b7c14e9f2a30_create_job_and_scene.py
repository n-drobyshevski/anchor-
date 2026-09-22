"""create job and scene, add message.scene_id and message.kind

Milestone 2a (phase-2 plan sections 3 and 4). A new revision on top of
Phase 1's head; Phase 1 revisions are never edited.

Reversible: downgrade drops the two new message columns and both new
tables, in FK-safe order. The data loss on downgrade is scenes and
queued jobs, both of which are derived/transient -- no chat history is
touched.

Revision ID: b7c14e9f2a30
Revises: aed45828669b
Create Date: 2026-09-22 08:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7c14e9f2a30'
down_revision: Union[str, Sequence[str], None] = 'aed45828669b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'scene',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('summary', sa.String(), nullable=True),
        sa.Column('message_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'job',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('payload', sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('dedup_key', sa.String(), nullable=True),
        sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('status', sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dedup_key'),
    )
    op.create_index('ix_job_status_run_after', 'job', ['status', 'run_after'])

    # server_default on kind, not just a Python-side default: existing
    # Phase 1 rows need a value for the NOT NULL to hold, and 'chat' is
    # the right one for every row written before scenes existed.
    op.add_column('message', sa.Column('scene_id', sa.BigInteger(), nullable=True))
    op.add_column(
        'message',
        sa.Column('kind', sa.String(), server_default=sa.text("'chat'"), nullable=False),
    )
    op.create_foreign_key('fk_message_scene_id', 'message', 'scene', ['scene_id'], ['id'])
    op.create_check_constraint(
        'ck_message_kind',
        'message',
        "kind in ('chat', 'checkin', 'welfare', 'canned', 'system')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_message_kind', 'message', type_='check')
    op.drop_constraint('fk_message_scene_id', 'message', type_='foreignkey')
    op.drop_column('message', 'kind')
    op.drop_column('message', 'scene_id')

    op.drop_index('ix_job_status_run_after', table_name='job')
    op.drop_table('job')
    op.drop_table('scene')
