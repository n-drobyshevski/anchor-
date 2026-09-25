"""add user_state.callback_scene

Milestone 5e (phase-5 plan sections 2, 3 and 11a). The last scene that
got a callback ("## Можно вспомнить"), so app/core/callbacks.py can tell
"this scene already had its one callback" from "this is a new scene,
check again" without a second table. Written only by
app/core/callbacks.py's own targeted UPDATE (`mark_delivered`) -- never
through update_state(), the same narrow-writer pattern app/core/
voice.py's `nickname_last` already established (f1a2c8e6d904).

**No foreign key**, deliberately, though the plan allows one. This
column lives on `user_state`, a table app/core/purge.py *keeps*, while
`scene` is one it *purges*. purge.py's TRUNCATE is intentionally
CASCADE-free, and Postgres refuses to TRUNCATE a table that is
referenced by a foreign key from a table not included in the same
statement, regardless of that FK's ON DELETE action -- confirmed while
building this migration (`TRUNCATE ... user_state references scene`).
A plain nullable bigint sidesteps that failure mode entirely;
`reset_values()` already nulls this column on every `/delete`, which is
what an ON DELETE SET NULL would have bought in any case.

Revision ID: c1a5e9f4b6d2
Revises: ab0ad60cd7ec
Create Date: 2026-09-23 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c1a5e9f4b6d2'
down_revision: Union[str, Sequence[str], None] = 'ab0ad60cd7ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('user_state', sa.Column('callback_scene', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user_state', 'callback_scene')
