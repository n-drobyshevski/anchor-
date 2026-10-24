"""study_job.manual: /interests run jobs count against their own daily quota

`/interests run [номер]` researches one saved topic on demand. It reuses
the /study queue path (a `kind='study'` job, a RESEARCH queue row), but
it must not spend the shared daily /study slot that idle research, /study
itself and the lens taps compete for, so those jobs are marked:

- `study_job.manual`: true for a job a `/interests run` made, false for
  every other (default false, so existing rows stay in the shared quota).
  The /study quota counts `manual = false` rows; the manual quota
  (`RESEARCH_MANUAL_PER_DAY`) counts `manual = true` ones.
- `ck_study_job_manual`: `not manual or kind = 'study'`. A `/read` is
  never manual. Written this way rather than by listing packets, so it
  needs no update when `PACKETS` grows.

`debug.study_job` gains `manual`: how many manual runs a day is a count
over a flag, nothing the user wrote.

Reversible. Downgrade puts the view back and drops the constraint and
the column.

Revision ID: c2e7a9f4b1d6
Revises: d4f8a2c6e1b3
Create Date: 2026-10-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c2e7a9f4b1d6'
down_revision: Union[str, Sequence[str], None] = 'd4f8a2c6e1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'
# Migration 9e4b2c7a1f05's columns, then the new one at the end
# (CREATE OR REPLACE VIEW may only add columns after the existing ones).
VIEW_BEFORE = (
    'id, kind, status, searches_used, pins_used, usd_cost, error_code, local_date, '
    'created_at, finished_at'
)
VIEW_AFTER = VIEW_BEFORE + ', manual'


def _grant() -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                GRANT SELECT ON debug.study_job TO {DEBUG_ROLE};
            END IF;
        END
        $$
    """)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'study_job',
        sa.Column('manual', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.create_check_constraint(
        'ck_study_job_manual', 'study_job', "not manual or kind = 'study'",
    )
    op.execute(f'CREATE OR REPLACE VIEW debug.study_job AS SELECT {VIEW_AFTER} FROM public.study_job')
    _grant()


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP VIEW IF EXISTS debug.study_job')
    op.execute(f'CREATE VIEW debug.study_job AS SELECT {VIEW_BEFORE} FROM public.study_job')
    _grant()
    op.drop_constraint('ck_study_job_manual', 'study_job', type_='check')
    op.drop_column('study_job', 'manual')
