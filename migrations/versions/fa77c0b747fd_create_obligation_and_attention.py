"""create obligation; add user_state.attention; widen ck_proposal_field

Phase 5 (spec 2026-09-25), slices 3-4.

- `obligation` is the debt queue: what the user still owes, oldest
  first. At most `MAX_OPEN` (5) rows are open at a time; that
  cap is enforced in app/core/obligations.py, since a CHECK cannot count
  rows. The partial unique index on (kind, due_local_date) for
  kind='checkin' makes the missed-check-in sweep idempotent.
- `user_state.attention` / `attention_until` are Anchor's own
  scarce-attention mode (app/core/attention.py). 'short' only adds a
  prompt flag; nothing here can make an inbound turn go unanswered.
- `ck_proposal_field` gains 'obligation': the extractor may propose a
  debt, and only a button press opens one.

Reversible.

Revision ID: fa77c0b747fd
Revises: 5c8e1d2b7a94
Create Date: 2026-09-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'fa77c0b747fd'
down_revision: Union[str, Sequence[str], None] = '5c8e1d2b7a94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_FIELD = "field in ('due_action', 'focus_on', 'rule', 'standing_order')"
_NEW_FIELD = "field in ('due_action', 'focus_on', 'rule', 'standing_order', 'obligation')"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'obligation',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('status', sa.String(), server_default=sa.text("'open'"), nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('due_local_date', sa.Date(), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_mentioned_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('char_length("text") <= 200', name='ck_obligation_text_length'),
        sa.CheckConstraint(
            "kind in ('checkin', 'focus', 'promised', 'missed', 'custom')",
            name='ck_obligation_kind',
        ),
        sa.CheckConstraint(
            "source in ('user', 'checkin', 'command', 'proposal')", name='ck_obligation_source'
        ),
        sa.CheckConstraint("status in ('open', 'done', 'dropped')", name='ck_obligation_status'),
        sa.CheckConstraint(
            "(status = 'open') = (closed_at is null)", name='ck_obligation_closed_iff_not_open'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_obligation_status_opened', 'obligation', ['status', 'opened_at'])
    op.create_index(
        'uq_obligation_checkin_date',
        'obligation',
        ['kind', 'due_local_date'],
        unique=True,
        postgresql_where=sa.text("kind = 'checkin'"),
    )

    op.add_column(
        'user_state',
        sa.Column('attention', sa.String(), server_default=sa.text("'present'"), nullable=False),
    )
    op.add_column(
        'user_state', sa.Column('attention_until', sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        'ck_user_state_attention', 'user_state', "attention in ('present', 'short')"
    )

    op.drop_constraint('ck_proposal_field', 'proposal', type_='check')
    op.create_check_constraint('ck_proposal_field', 'proposal', _NEW_FIELD)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM proposal WHERE field = 'obligation'")
    op.drop_constraint('ck_proposal_field', 'proposal', type_='check')
    op.create_check_constraint('ck_proposal_field', 'proposal', _OLD_FIELD)

    op.drop_constraint('ck_user_state_attention', 'user_state', type_='check')
    op.drop_column('user_state', 'attention_until')
    op.drop_column('user_state', 'attention')

    op.drop_index('uq_obligation_checkin_date', table_name='obligation')
    op.drop_index('ix_obligation_status_opened', table_name='obligation')
    op.drop_table('obligation')
