"""create outbound, add user_state counters and message.outbound_id

Milestone 3a (phase-3 plan section 4). The schema Phase 3's proactive
messages stand on.

Three things worth knowing about this revision:

1. `uq_outbound_kind_date_bucket` is the exactly-once guarantee. Two
   overlapping processes during a Railway rollout, a duplicate
   heartbeat, or a restart mid-morning all collapse into one message
   because the second insert conflicts. The constraint is the
   mechanism; nothing in the send path relies on worker concurrency
   being 1.

2. `outbound` and `message` reference each other. `outbound.message_id`
   is therefore added as a separate ALTER after both tables exist,
   rather than inline -- there is no ordering of two CREATE TABLEs that
   satisfies a cycle.

3. `ck_message_kind` is dropped and recreated to admit 'outbound'.
   Postgres has no ALTER ... MODIFY CONSTRAINT, so this is drop-then-
   add; it is a catalogue-only change on a small table.

Reversible. The downgrade destroys the record of every proactive
message ever planned or sent, and resets the back-off counters -- after
which the bot would happily re-send today's morning message, since the
row proving it already went out is gone. Not a downgrade to run on a
live deployment without first setting OUTBOUND_ENABLED=false.

Revision ID: f3a91c7d02b5
Revises: e5b71c93d820
Create Date: 2026-09-22 14:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f3a91c7d02b5'
down_revision: Union[str, Sequence[str], None] = 'e5b71c93d820'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MESSAGE_KINDS_BEFORE = "kind in ('chat', 'checkin', 'welfare', 'canned', 'system')"
_MESSAGE_KINDS_AFTER = (
    "kind in ('chat', 'checkin', 'welfare', 'canned', 'system', 'outbound')"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'outbound',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('bucket', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('planned_for', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(), server_default=sa.text("'planned'"), nullable=False),
        sa.Column('skip_reason', sa.String(), nullable=True),
        sa.Column('tick_note', sa.String(), nullable=True),
        sa.Column('message_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind in ('morning', 'evening_nag', 'silence', 'tick')", name='ck_outbound_kind'
        ),
        sa.CheckConstraint(
            "status in ('planned', 'sent', 'skipped', 'cancelled', 'failed')",
            name='ck_outbound_status',
        ),
        sa.CheckConstraint(
            'char_length("tick_note") <= 120', name='ck_outbound_tick_note_length'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kind', 'local_date', 'bucket', name='uq_outbound_kind_date_bucket'),
    )
    op.create_index(
        'ix_outbound_status_planned_for', 'outbound', ['status', 'planned_for'], unique=False
    )
    op.create_index('ix_outbound_local_date', 'outbound', ['local_date'], unique=False)

    # The cycle: outbound.message_id -> message.id, and below,
    # message.outbound_id -> outbound.id.
    op.create_foreign_key(
        'fk_outbound_message_id', 'outbound', 'message', ['message_id'], ['id']
    )

    op.add_column(
        'user_state', sa.Column('quiet_until', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'user_state', sa.Column('last_user_msg_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'user_state', sa.Column('last_outbound_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'user_state',
        sa.Column('ignored_in_row', sa.Integer(), server_default=sa.text('0'), nullable=False),
    )
    op.add_column(
        'user_state', sa.Column('welfare_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        'ck_user_state_ignored_non_negative', 'user_state', 'ignored_in_row >= 0'
    )

    op.add_column('message', sa.Column('outbound_id', sa.BigInteger(), nullable=True))
    op.create_unique_constraint('uq_message_outbound_id', 'message', ['outbound_id'])
    op.create_foreign_key(
        'fk_message_outbound_id', 'message', 'outbound', ['outbound_id'], ['id']
    )

    op.drop_constraint('ck_message_kind', 'message', type_='check')
    op.create_check_constraint('ck_message_kind', 'message', _MESSAGE_KINDS_AFTER)


def downgrade() -> None:
    """Downgrade schema."""
    # Any 'outbound' message rows must go before the constraint that
    # forbids them is restored, or the ADD CONSTRAINT fails on the
    # existing data. They are proactive messages whose outbound row is
    # about to be dropped anyway.
    op.execute("delete from message where kind = 'outbound'")
    op.drop_constraint('ck_message_kind', 'message', type_='check')
    op.create_check_constraint('ck_message_kind', 'message', _MESSAGE_KINDS_BEFORE)

    op.drop_constraint('fk_message_outbound_id', 'message', type_='foreignkey')
    op.drop_constraint('uq_message_outbound_id', 'message', type_='unique')
    op.drop_column('message', 'outbound_id')

    op.drop_constraint('ck_user_state_ignored_non_negative', 'user_state', type_='check')
    op.drop_column('user_state', 'welfare_at')
    op.drop_column('user_state', 'ignored_in_row')
    op.drop_column('user_state', 'last_outbound_at')
    op.drop_column('user_state', 'last_user_msg_at')
    op.drop_column('user_state', 'quiet_until')

    op.drop_constraint('fk_outbound_message_id', 'outbound', type_='foreignkey')
    op.drop_index('ix_outbound_local_date', table_name='outbound')
    op.drop_index('ix_outbound_status_planned_for', table_name='outbound')
    op.drop_table('outbound')
