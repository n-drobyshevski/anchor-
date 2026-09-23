"""create weekly_review, review_proposal and persona_amendment

Milestone 5d (phase-5 plan sections 3, 8 and 9; implementation plan's
"Files" list). The weekly review's own three tables:

- `weekly_review` -- one row per local week (`week_start` is the local
  Monday, unique), holding the safety model's validated JSON analysis
  and the id of the persona message that carried it.
- `review_proposal` -- what the review suggested, each sent as its own
  card: a `standing_order` (which also gets its own `standing_order`
  row via `app/core/orders.py`'s `propose()`) or a `persona_note`
  (which becomes a `persona_amendment` on adoption).
- `persona_amendment` -- a `persona_note` proposal the user adopted,
  `trial` until `amendment_trial` (5d's job) either activates or fails
  it against the blocking eval subset with an independent judge.
  `persona_sha` is the hash of `persona.md` at adoption time, so
  `/amendments` can flag a stale amendment after a manual persona edit
  without ever rewriting the file itself -- `persona.md` is never
  written by this codebase.

`ck_review_proposal_kind`/`_status` and `ck_persona_amendment_status`
mirror `standing_order`'s own enum-as-CHECK convention. Length checks
match the implementation plan's validation limits verbatim: wins/misses
≤160 (not enforced here -- they live inside the `analysis` jsonb, not a
column), `review_proposal.text` ≤200 and `.reason` ≤160 like
`standing_order.text`, `persona_amendment.text` ≤200.

Revision ID: dcc3e9f3b709
Revises: a4d81f37c9e2
Create Date: 2026-09-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'dcc3e9f3b709'
down_revision: Union[str, Sequence[str], None] = 'a4d81f37c9e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'weekly_review',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('week_start', sa.Date(), nullable=False),
        sa.Column('analysis', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(['message_id'], ['message.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('week_start', name='uq_weekly_review_week_start'),
    )

    op.create_table(
        'review_proposal',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('review_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('status', sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind in ('standing_order', 'persona_note')", name='ck_review_proposal_kind'
        ),
        sa.CheckConstraint(
            "status in ('pending', 'adopted', 'rejected', 'expired')",
            name='ck_review_proposal_status',
        ),
        sa.CheckConstraint('char_length("text") <= 200', name='ck_review_proposal_text_length'),
        sa.CheckConstraint(
            'reason is null or char_length(reason) <= 160', name='ck_review_proposal_reason_length'
        ),
        sa.ForeignKeyConstraint(['review_id'], ['weekly_review.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_review_proposal_status', 'review_proposal', ['status'])

    op.create_table(
        'persona_amendment',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('proposal_id', sa.BigInteger(), nullable=True),
        sa.Column('eval_report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('persona_sha', sa.String(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('trial', 'active', 'failed', 'revoked')",
            name='ck_persona_amendment_status',
        ),
        sa.CheckConstraint('char_length("text") <= 200', name='ck_persona_amendment_text_length'),
        sa.ForeignKeyConstraint(['proposal_id'], ['review_proposal.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_persona_amendment_status', 'persona_amendment', ['status'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_persona_amendment_status', table_name='persona_amendment')
    op.drop_table('persona_amendment')
    op.drop_index('ix_review_proposal_status', table_name='review_proposal')
    op.drop_table('review_proposal')
    op.drop_table('weekly_review')
