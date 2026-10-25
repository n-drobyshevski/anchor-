"""llm_model_override: the model chosen for each role from the menu

Runtime model choice (docs/decisions.md, "Runtime model choice"): the
model each LLM role runs on has always come from env (`LLM_MODEL`,
`LLM_MODEL_CHEAP`, `LLM_MODEL_SAFETY`, `LLM_MODEL_JUDGE`, `GARDEN_MODEL`,
`INTEREST_SUGGEST_MODEL`, `WELFARE_SHADOW_MODEL`), fixed at startup, so
changing one meant editing env and redeploying. ⚙️ Настройки › 🧠 Модели
now switches it from Telegram, and the switch has to survive a restart:
this table is where it lives. app/llm/models.py's resolver reads it at
startup and answers `effective(role)` from an in-process cache.

One row per overridden role; no row means "the env default" (or, for the
garden and the topic suggester, the fallback chain app/llm/models.py
spells out). ↩️ «По умолчанию» deletes the row.

- `role` is the primary key, one of the seven roles (`ck_llm_model_
  override_role`, which app/llm/models.py's `ROLES` must equal):
  `persona`, `cheap`, `safety`, `judge`, `garden`, `suggest`,
  `welfare_shadow`.
- `model` is the OpenRouter model id. It may be the empty string for
  `welfare_shadow` only (`ck_llm_model_override_model`): that role is
  off when no model is set, so an override of "" means "switched off in
  the menu even though env names a model". Every other role always
  needs a model.
- `price_in`, `price_cached` and `price_out` are USD per million
  tokens, nullable: what OpenRouter's catalog said when the model was
  picked (app/llm/catalog.py). app/core/spend.py bills a call at these
  prices when the model has an override row with prices; with none, the
  env price settings apply as before, and the cost OpenRouter reports
  for a call still wins over either. A curated pick made while
  OpenRouter was unreachable stores no prices.
- `updated_at` is when the override was set.

Every change is also audited as a `state_change` row (field
`model:<role>`, source `button`); the table keeps only the present.

**Debug view** (granted to `anchor_debug`, as 9e4b2c7a1f05 explains),
content-free columns only: `debug.llm_model_override` has role, model
and updated_at -- the model id is an OpenRouter catalog name, nothing
the user or the model wrote. The prices are left out only because
debugging a bad switch does not need them.

`/export` includes the table and `/delete` truncates it: a model choice
is a setting of this user's, and "delete all my data" puts every setting
back to its default (like `claude_write_limit`).

Reversible: downgrade drops the view, then the table; every role goes
back to its env model.

Revision ID: d8a3f6c1b925
Revises: b4e7d2a9c613
Create Date: 2026-10-25 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd8a3f6c1b925'
down_revision: Union[str, Sequence[str], None] = 'b4e7d2a9c613'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEBUG_ROLE = 'anchor_debug'

ROLES = "'persona', 'cheap', 'safety', 'judge', 'garden', 'suggest', 'welfare_shadow'"

# Explicit column lists, never `*` (9e4b2c7a1f05's rule).
DEBUG_VIEWS: dict[str, str] = {
    'llm_model_override': 'role, model, updated_at',
}


def _grant_debug(views: Sequence[str]) -> None:
    grants = '\n'.join(f'GRANT SELECT ON debug.{name} TO {DEBUG_ROLE};' for name in views)
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{DEBUG_ROLE}') THEN
                {grants}
            END IF;
        END
        $$
    """)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'llm_model_override',
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=False),
        sa.Column('price_in', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('price_cached', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column('price_out', sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.CheckConstraint(f'role in ({ROLES})', name='ck_llm_model_override_role'),
        sa.CheckConstraint(
            "model <> '' or role = 'welfare_shadow'", name='ck_llm_model_override_model'
        ),
        sa.CheckConstraint(
            '(price_in is null or price_in >= 0) and (price_cached is null or price_cached >= 0)'
            ' and (price_out is null or price_out >= 0)',
            name='ck_llm_model_override_price',
        ),
        sa.PrimaryKeyConstraint('role'),
    )

    for name, columns in DEBUG_VIEWS.items():
        op.execute(f'CREATE VIEW debug.{name} AS SELECT {columns} FROM public.{name}')
    _grant_debug(list(DEBUG_VIEWS))


def downgrade() -> None:
    """Downgrade schema."""
    for name in DEBUG_VIEWS:
        op.execute(f'DROP VIEW IF EXISTS debug.{name}')
    op.drop_table('llm_model_override')
