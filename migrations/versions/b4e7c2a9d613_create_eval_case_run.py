"""eval reliability (E1a): eval_case_run

anchor-eval-reliability-plan.md section 3.3, milestone E1a. One table:

- `eval_case_run`: one row per blocking case per amendment trial or
  canary -- how many times the case ran and how the runs ended
  (`passed`, `check_failed`, `judge_failed`, `unusable`), with the
  persona and judge model ids they ran on. The canary reads it to say
  which cases are saturated, live or dead over the current model pair
  (section 3.4).

No text: ids, counts, model ids and a time. Written and read only by
app/core/eval_history.py.

**Debug view** (granted to `anchor_debug` here, as b8d24f6e0a17
explains, and skipped when the role is absent): `debug.eval_case_run`,
whole -- there is no text in it.

Reversible. Downgrade drops the view, then the table.

Revision ID: b4e7c2a9d613
Revises: f3b8d1c6a2e9
Create Date: 2026-11-16 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b4e7c2a9d613'
down_revision: Union[str, Sequence[str], None] = 'f3b8d1c6a2e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'

# Explicit column lists, never `*` (9e4b2c7a1f05's rule).
EVAL_DEBUG_VIEWS: dict[str, str] = {
    'eval_case_run': (
        'id, at, source, source_id, case_id, runs, passed, check_failed, judge_failed, '
        'unusable, persona_model, judge_model'
    ),
}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'eval_case_run',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.String(), nullable=False),
        sa.Column('runs', sa.Integer(), nullable=False),
        sa.Column('passed', sa.Integer(), nullable=False),
        sa.Column('check_failed', sa.Integer(), nullable=False),
        sa.Column('judge_failed', sa.Integer(), nullable=False),
        sa.Column('unusable', sa.Integer(), nullable=False),
        sa.Column('persona_model', sa.String(), nullable=False),
        sa.Column('judge_model', sa.String(), nullable=False),
        sa.CheckConstraint("source in ('trial', 'canary')", name='ck_eval_case_run_source'),
        sa.CheckConstraint(
            'runs >= 1 and passed >= 0 and check_failed >= 0 and judge_failed >= 0'
            ' and unusable >= 0 and passed + check_failed + judge_failed + unusable = runs',
            name='ck_eval_case_run_counts',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_eval_case_run_models', 'eval_case_run', ['persona_model', 'judge_model', 'case_id']
    )

    for name, columns in EVAL_DEBUG_VIEWS.items():
        op.execute(f'CREATE VIEW debug.{name} AS SELECT {columns} FROM public.{name}')
    grants = '\n'.join(
        f'GRANT SELECT ON debug.{name} TO {DEBUG_ROLE};' for name in EVAL_DEBUG_VIEWS
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
    for name in EVAL_DEBUG_VIEWS:
        op.execute(f'DROP VIEW IF EXISTS debug.{name}')
    op.drop_index('ix_eval_case_run_models', table_name='eval_case_run')
    op.drop_table('eval_case_run')
