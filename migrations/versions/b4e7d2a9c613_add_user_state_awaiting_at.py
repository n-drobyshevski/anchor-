"""add user_state.awaiting_at

The menu redesign (docs/decisions.md, "Menu redesign"): a /menu button
that needs typed text («✏️ Запомнить», «✏️ Часовой пояс», ...) asks for
it and waits for the next message, by setting `awaiting = 'menu:<key>'`
(app/core/menu_prompt.py). Unlike the check-in note step, which expires
on the local-date boundary it already has, a menu prompt needs a clock
of its own: after MENU_PROMPT_TTL_MIN the next message is ordinary chat
again, so a prompt the user walked away from never swallows a later
line as a command. `awaiting_at` is that clock -- when the pending step
was opened.

Nullable, with no default: NULL means "nothing pending", which is the
singleton row's state the moment this column exists, and the state
app/core/checkin.py's `clear_awaiting` and a `/delete` return it to.
Not added to the debug view (9e4b2c7a1f05): it is a timestamp of a
button press, and the view's `awaiting` column already says whether a
step is open.

Revision ID: b4e7d2a9c613
Revises: a5d1c8e3f702
Create Date: 2026-10-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b4e7d2a9c613'
down_revision: Union[str, Sequence[str], None] = 'a5d1c8e3f702'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'user_state', sa.Column('awaiting_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('user_state', 'awaiting_at')
