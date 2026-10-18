"""the welfare shadow: one content-free row per turn a decision model
was asked the welfare question beside the classifier

app/core/welfare_shadow.py asks a typed-decision model (TypeSafe's Jev,
through OpenRouter's Decisions API) the same none/scene/real question
the welfare classifier answers, on the same turns, when
WELFARE_SHADOW_MODEL is set. Its answer is never acted on; this table
is where the two are put side by side.

- `welfare_shadow`: levels, confidences, the shadow's probability on
  `real`, both call times, both outcomes and the shadow's model. No
  text of any kind. Check constraints close every vocabulary and tie
  a level to an `ok` outcome on each side.
- `debug.welfare_shadow`: every column, granted to `anchor_debug`, so
  the comparison can be run in SQL without touching anything else.

Reversible. Downgrade drops the view and the table.

Revision ID: a7c3e1f9d2b4
Revises: 3d3efa0cbc9a
Create Date: 2026-10-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7c3e1f9d2b4'
down_revision: Union[str, Sequence[str], None] = '3d3efa0cbc9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'
LEVELS = "('none', 'scene', 'real')"
# Named, not `*`, like every other debug view: a column added to the
# table later must be added here on purpose, never by accident.
VIEW_COLUMNS = (
    'id, ts, local_date, primary_outcome, primary_level, primary_confidence, primary_ms, '
    'shadow_model, shadow_outcome, shadow_level, shadow_confidence, shadow_p_real, shadow_ms'
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'welfare_shadow',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('local_date', sa.Date(), nullable=False),
        sa.Column('primary_outcome', sa.String(), nullable=False),
        sa.Column('primary_level', sa.String(), nullable=True),
        sa.Column('primary_confidence', sa.Float(), nullable=True),
        sa.Column('primary_ms', sa.Integer(), nullable=False),
        sa.Column('shadow_model', sa.String(), nullable=False),
        sa.Column('shadow_outcome', sa.String(), nullable=False),
        sa.Column('shadow_level', sa.String(), nullable=True),
        sa.Column('shadow_confidence', sa.Float(), nullable=True),
        sa.Column('shadow_p_real', sa.Float(), nullable=True),
        sa.Column('shadow_ms', sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "primary_outcome in ('ok', 'parse_fail', 'timeout', 'error')",
            name='ck_welfare_shadow_primary_outcome',
        ),
        sa.CheckConstraint(
            "shadow_outcome in ('ok', 'timeout', 'error')",
            name='ck_welfare_shadow_shadow_outcome',
        ),
        sa.CheckConstraint(f"primary_level in {LEVELS}", name='ck_welfare_shadow_primary_level'),
        sa.CheckConstraint(f"shadow_level in {LEVELS}", name='ck_welfare_shadow_shadow_level'),
        sa.CheckConstraint(
            "(primary_outcome = 'ok') = (primary_level is not null)",
            name='ck_welfare_shadow_primary_answered',
        ),
        sa.CheckConstraint(
            "(shadow_outcome = 'ok') = (shadow_level is not null)",
            name='ck_welfare_shadow_shadow_answered',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_welfare_shadow_local_date', 'welfare_shadow', ['local_date'])

    op.execute(f'CREATE VIEW debug.welfare_shadow AS SELECT {VIEW_COLUMNS} FROM public.welfare_shadow')
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                GRANT SELECT ON debug.welfare_shadow TO {DEBUG_ROLE};
            END IF;
        END
        $$
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP VIEW IF EXISTS debug.welfare_shadow')
    op.drop_index('ix_welfare_shadow_local_date', table_name='welfare_shadow')
    op.drop_table('welfare_shadow')
