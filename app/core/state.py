"""Read/update the singleton `user_state` row, with an audit trail.

Every field write goes through update_state() so a `state_change` row
(field, old_value, new_value, source) is always recorded in the same
transaction as the change itself (plan section 5). `source` is one of
command|pause|system, matching the callers introduced across 1b-1d:
1b's commands, 1d's pause words, and startup/system-driven changes.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StateChange, UserState

STATE_ID = 1

Source = Literal["command", "pause", "system"]


async def get_state(session: AsyncSession) -> UserState:
    """Read the single user_state row.

    Raises sqlalchemy.exc.NoResultFound if the startup upsert (app/
    startup.py) has not run yet — there is deliberately no fallback
    default here, since a missing row means startup was skipped.
    """
    result = await session.execute(select(UserState).where(UserState.id == STATE_ID))
    return result.scalar_one()


async def update_state(session: AsyncSession, field: str, value: Any, source: Source) -> UserState:
    """Set one field on user_state and record a state_change audit row.

    Both writes commit together. `field` must name an existing
    UserState column; old/new values are stringified for the audit log
    since state_change stores them as text regardless of the field's
    real type.
    """
    state = await get_state(session)
    old_value = getattr(state, field)
    setattr(state, field, value)
    session.add(
        StateChange(
            field=field,
            old_value=None if old_value is None else str(old_value),
            new_value=None if value is None else str(value),
            source=source,
        )
    )
    await session.commit()
    await session.refresh(state)
    return state
