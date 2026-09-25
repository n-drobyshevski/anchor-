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
Revises: a7c3f281b6d4
Create Date: 2026-09-23 10:00:00.000000

Re-parented from its original `d1b83f6c204e` onto the web-UI head
`a7c3f281b6d4` to resolve the two-heads split created when this branch
(forked from `d1b83f6c204e`, before phase 5/6 and the web UI existed)
was merged alongside `claude/chatbot-web-interface-w184r5`'s own chain
(`d1b83f6c204e` -> ... -> `a7c3f281b6d4`), a sibling descendant of the
same `d1b83f6c204e` revision. Safe to re-parent: this migration only
`op.create_table`s three brand-new tables (no `op.alter_column`,
`op.add_column`, or `op.create_foreign_key` touching any existing
table, and no FK at all -- see this file's own docstring above), so
nothing in the intervening web-UI revisions conflicts with it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3f6c1d8e945'
down_revision: Union[str, Sequence[str], None] = 'a7c3f281b6d4'
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
        # A random key, independent of `id` -- app/planner/jobs.py's
        # clientRequestId is derived from this, not from `id` directly,
        # because `id` is not stable across a /delete purge (purge.py
        # TRUNCATEs with RESTART IDENTITY, so a later row can reuse an
        # old id and collide with the planner's
        # (owner_id, client_request_id) unique index).
        sa.Column(
            'request_key',
            sa.String(),
            server_default=sa.text('gen_random_uuid()::text'),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind in ('create_task', 'create_event', 'complete_task')",
            name='ck_planner_action_kind',
        ),
        sa.CheckConstraint(
            "status in ('pending', 'accepted', 'rejected', 'expired', 'written', 'failed')",
            name='ck_planner_action_status',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_planner_action_status', 'planner_action', ['status'], unique=False
    )
    op.create_index(
        'ix_planner_action_request_key', 'planner_action', ['request_key'], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_planner_action_request_key', table_name='planner_action')
    op.drop_index('ix_planner_action_status', table_name='planner_action')
    op.drop_table('planner_action')
    op.drop_table('planner_snapshot')
    op.drop_table('planner_credential')
