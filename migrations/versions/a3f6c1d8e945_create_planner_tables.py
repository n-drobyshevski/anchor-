"""create planner_credential, planner_snapshot, planner_action

P2 (design review section 2.1): Anchor's read path into the planner,
plus the OAuth link flow. All three tables ship in one migration even
though P2 itself only ever writes the first two -- `planner_action` is
P3's write-confirmation schema, created now so a later milestone is a
code-only change.

`planner_credential` and `planner_snapshot` are singleton rows (id
pinned to 1 by a check constraint), the same pattern `user_state`
already uses in this schema.

Reversible. The downgrade drops all three tables outright: there is no
"reset to defaults" for an OAuth grant the way there is for user_state,
so undoing this migration means Anchor loses the planner link and its
cached agenda entirely, and PLANNER_ENABLED must be off before running
it on a live deployment.

Revision ID: a3f6c1d8e945
Revises: d1b83f6c204e
Create Date: 2026-09-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3f6c1d8e945'
down_revision: Union[str, Sequence[str], None] = 'd1b83f6c204e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'planner_credential',
        sa.Column('id', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('access_token', sa.String(), nullable=False),
        sa.Column('refresh_token', sa.String(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('client_id', sa.String(), nullable=True),
        sa.Column('status', sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('notified', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.CheckConstraint('id = 1', name='ck_planner_credential_id_singleton'),
        sa.CheckConstraint(
            "status in ('active', 'revoked')", name='ck_planner_credential_status'
        ),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'planner_snapshot',
        sa.Column('id', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint('id = 1', name='ck_planner_snapshot_id_singleton'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'planner_action',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('tg_message_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind in ('create_task', 'create_event', 'complete_task')",
            name='ck_planner_action_kind',
        ),
        sa.CheckConstraint(
            "status in ('pending', 'accepted', 'rejected', 'expired')",
            name='ck_planner_action_status',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_planner_action_status', 'planner_action', ['status'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_planner_action_status', table_name='planner_action')
    op.drop_table('planner_action')
    op.drop_table('planner_snapshot')
    op.drop_table('planner_credential')
