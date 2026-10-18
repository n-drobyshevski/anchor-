"""study_card corroboration: cross-checking a research's cards between its pages

A research reads up to two pages and distills each alone, so a claim one
page makes looked the same as one two sources agree on. Lens research
now checks every card against the research's other pages of another
publisher (app/research/crosscheck.py) and keeps the verdict:

- `study_card.corroboration`: `supported`, `contradicted`, `unverified`
  (checked, nothing settled it) or NULL (never checked);
- `study_card.corroboration_clip_id`: the page that settled it (SET NULL
  with the clip);
- `study_card.corroboration_quote`: that page's verbatim quote, at most
  240 characters, present exactly when the verdict is `supported` or
  `contradicted`. Web text, like `quote`.

`debug.study_card` gains the verdict and the clip id, never the quote:
how often a second source agrees is something to debug, and both are
codes and ids.

Reversible. Downgrade puts the view back and drops the columns.

Revision ID: d4f8a2c6e1b3
Revises: a7c3e1f9d2b4
Create Date: 2026-10-18 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4f8a2c6e1b3'
down_revision: Union[str, Sequence[str], None] = 'a7c3e1f9d2b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'
# Migration 9e4b2c7a1f05's columns, then the two new ones at the end
# (CREATE OR REPLACE VIEW may only add columns after the existing ones).
VIEW_BEFORE = (
    'id, job_id, clip_id, kind, risk_model, risk_rules, risk_final, rule_hits, '
    'status, memory_id, created_at, decided_at'
)
VIEW_AFTER = VIEW_BEFORE + ', corroboration, corroboration_clip_id'


def _grant() -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                GRANT SELECT ON debug.study_card TO {DEBUG_ROLE};
            END IF;
        END
        $$
    """)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('study_card', sa.Column('corroboration', sa.String(), nullable=True))
    op.add_column('study_card', sa.Column('corroboration_clip_id', sa.BigInteger(), nullable=True))
    op.add_column('study_card', sa.Column('corroboration_quote', sa.String(), nullable=True))
    op.create_foreign_key(
        'fk_study_card_corroboration_clip_id', 'study_card', 'study_clip',
        ['corroboration_clip_id'], ['id'], ondelete='SET NULL',
    )
    op.create_check_constraint(
        'ck_study_card_corroboration', 'study_card',
        "corroboration is null "
        "or corroboration in ('supported', 'contradicted', 'unverified')",
    )
    op.create_check_constraint(
        'ck_study_card_corroboration_quote_length', 'study_card',
        'char_length(corroboration_quote) <= 240',
    )
    op.create_check_constraint(
        'ck_study_card_corroboration_quote', 'study_card',
        "(corroboration_quote is null) "
        "= (corroboration is null or corroboration = 'unverified')",
    )
    op.execute(f'CREATE OR REPLACE VIEW debug.study_card AS SELECT {VIEW_AFTER} FROM public.study_card')
    _grant()


def downgrade() -> None:
    """Downgrade schema."""
    op.execute('DROP VIEW IF EXISTS debug.study_card')
    op.execute(f'CREATE VIEW debug.study_card AS SELECT {VIEW_BEFORE} FROM public.study_card')
    _grant()
    op.drop_constraint('ck_study_card_corroboration_quote', 'study_card', type_='check')
    op.drop_constraint('ck_study_card_corroboration_quote_length', 'study_card', type_='check')
    op.drop_constraint('ck_study_card_corroboration', 'study_card', type_='check')
    op.drop_constraint('fk_study_card_corroboration_clip_id', 'study_card', type_='foreignkey')
    op.drop_column('study_card', 'corroboration_quote')
    op.drop_column('study_card', 'corroboration_clip_id')
    op.drop_column('study_card', 'corroboration')
