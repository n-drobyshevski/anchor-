"""eval reliability (E1c): judge_label, judge_notice

anchor-eval-reliability-plan.md sections 5.2 and 5.4, milestone E1c.
Two tables:

- `judge_label`: the user's own rating of one core rubric item on one
  persona reply (`user_ok`), with the judge's score for it
  (`judge_score`, NULL when the judgement was unusable) and the judge
  model. Unique on `(message_id, item)`; `message_id` goes NULL if the
  message is deleted.
- `judge_notice`: per rubric item, whether the user was last told it is
  advisory, so the Telegram notice of a transition goes out once and a
  restart does not repeat it.

No text in either: ids, booleans, scores, model ids and times. Written
and read only by app/core/judge_calibration.py.

**Debug views** (granted to `anchor_debug` here, as b8d24f6e0a17
explains, and skipped when the role is absent): `debug.judge_label` and
`debug.judge_notice`, whole -- there is no text in them.

Reversible. Downgrade drops the views, then the tables.

Revision ID: c7d3a1e5f824
Revises: b4e7c2a9d613
Create Date: 2026-11-16 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c7d3a1e5f824'
down_revision: Union[str, Sequence[str], None] = 'b4e7c2a9d613'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'

# Explicit column lists, never `*` (9e4b2c7a1f05's rule).
JUDGE_DEBUG_VIEWS: dict[str, str] = {
    'judge_label': 'id, message_id, item, user_ok, judge_score, judge_model, created_at',
    'judge_notice': 'item, advisory, announced_at',
}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'judge_label',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=True),
        sa.Column('item', sa.String(), nullable=False),
        sa.Column('user_ok', sa.Boolean(), nullable=False),
        sa.Column('judge_score', sa.Integer(), nullable=True),
        sa.Column('judge_model', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['message_id'], ['message.id'], ondelete='SET NULL'),
        sa.CheckConstraint(
            'judge_score is null or (judge_score >= 1 and judge_score <= 5)',
            name='ck_judge_label_score',
        ),
        sa.UniqueConstraint('message_id', 'item', name='uq_judge_label_message_item'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_judge_label_model_item', 'judge_label', ['judge_model', 'item'])
    op.create_index('ix_judge_label_created_at', 'judge_label', ['created_at'])
    op.create_table(
        'judge_notice',
        sa.Column('item', sa.String(), nullable=False),
        sa.Column('advisory', sa.Boolean(), nullable=False),
        sa.Column('announced_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('item'),
    )

    for name, columns in JUDGE_DEBUG_VIEWS.items():
        op.execute(f'CREATE VIEW debug.{name} AS SELECT {columns} FROM public.{name}')
    grants = '\n'.join(
        f'GRANT SELECT ON debug.{name} TO {DEBUG_ROLE};' for name in JUDGE_DEBUG_VIEWS
    )
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                {grants}
            END IF;
        END
        $$
    """)


def downgrade() -> None:
    """Downgrade schema."""
    for name in JUDGE_DEBUG_VIEWS:
        op.execute(f'DROP VIEW IF EXISTS debug.{name}')
    op.drop_table('judge_notice')
    op.drop_index('ix_judge_label_created_at', table_name='judge_label')
    op.drop_index('ix_judge_label_model_item', table_name='judge_label')
    op.drop_table('judge_label')
