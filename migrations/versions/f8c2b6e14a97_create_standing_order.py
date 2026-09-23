"""create standing_order and checkin_order_result

Milestone 5c (phase-5 plan sections 3 and 7; implementation plan's
"Files" list). Negotiated standing orders: the extractor and (from 5d)
the weekly review can propose one, the user can also author one
directly with /order, and an active order shows up in the prompt and as
one step in the evening check-in. There is no penalty logic anywhere --
a miss is only ever mentioned, never enforced.

`ck_standing_order_status` names the seven statuses `app/db/models.py`'s
own docstring walks through: `proposed` -> (`awaiting_counter` ->
`countered`) -> `active` | `declined`, or `expired` off the two waiting
states, and `retired` once an accepted order is removed.

`ck_standing_order_weekly_needs_weekday` is a two-way iff, not a
one-way check: a `weekly` order without a weekday cannot be compared
against a check-in's local weekday, and a non-`weekly` order carrying
one would be a number nothing ever reads. `ck_standing_order_not_self_
counter` only rules out the degenerate case a CHECK constraint can
actually see (a row naming itself); "a counter can't itself be
countered" depends on the *original* row's own `counter_of`, which is
enforced in app/core/orders.py's `start_counter`, not here.

`checkin_order_result` mirrors `checkin_order_result`'s own docstring:
primary key is `(checkin_id, order_id)`, both FKs `ON DELETE CASCADE`,
so an upsert against that key is what makes a replayed check-in-order
button idempotent.

Revision ID: f8c2b6e14a97
Revises: e7b1c4a9d803
Create Date: 2026-09-23 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f8c2b6e14a97'
down_revision: Union[str, Sequence[str], None] = 'e7b1c4a9d803'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'standing_order',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('cadence', sa.String(), nullable=False),
        sa.Column('weekday', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('counter_of', sa.BigInteger(), nullable=True),
        sa.Column('tg_message_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('char_length("text") <= 200', name='ck_standing_order_text_length'),
        sa.CheckConstraint(
            "cadence in ('daily', 'weekdays', 'weekly', 'once')",
            name='ck_standing_order_cadence',
        ),
        sa.CheckConstraint(
            "status in ('proposed', 'awaiting_counter', 'countered', 'active', "
            "'declined', 'retired', 'expired')",
            name='ck_standing_order_status',
        ),
        sa.CheckConstraint(
            "source in ('anchor', 'user', 'review')", name='ck_standing_order_source'
        ),
        sa.CheckConstraint(
            "(cadence = 'weekly') = (weekday is not null)",
            name='ck_standing_order_weekly_needs_weekday',
        ),
        sa.CheckConstraint(
            "weekday is null or weekday between 1 and 7",
            name='ck_standing_order_weekday_range',
        ),
        sa.CheckConstraint(
            "counter_of is null or counter_of <> id", name='ck_standing_order_not_self_counter'
        ),
        sa.ForeignKeyConstraint(['counter_of'], ['standing_order.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_standing_order_status', 'standing_order', ['status'])

    op.create_table(
        'checkin_order_result',
        sa.Column('checkin_id', sa.BigInteger(), nullable=False),
        sa.Column('order_id', sa.BigInteger(), nullable=False),
        sa.Column('result', sa.String(), nullable=False),
        sa.CheckConstraint("result in ('done', 'no')", name='ck_checkin_order_result_result'),
        sa.ForeignKeyConstraint(['checkin_id'], ['checkin.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['order_id'], ['standing_order.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('checkin_id', 'order_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('checkin_order_result')
    op.drop_index('ix_standing_order_status', table_name='standing_order')
    op.drop_table('standing_order')
