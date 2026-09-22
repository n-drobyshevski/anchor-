"""Read/update the singleton `user_state` row, with an audit trail.

Every `user_state` field write goes through update_state() so a
`state_change` row (field, old_value, new_value, source) is always
recorded in the same transaction as the change itself (plan section 5).

2b adds record_change() for the other kind of audit row: a change worth
recording that is *not* a user_state field at all, such as a deleted
memory. Those cannot go through update_state(), which reads and writes
an attribute on the singleton row. The audit table accommodates them
because every one of its columns is nullable.

`source` gains `button` in 2b (an inline keyboard press), joining 1b's
commands, 1d's pause words, and startup/system-driven changes.

**Never put content in an audit row.** `old_value` is exactly where a
future maintainer would helpfully record a deleted memory's text, and
plan section 11 explicitly requires the opposite: "`state_change`
records `memory <id> deleted` with no text". tests/test_memory.py
asserts it.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StateChange, UserState

STATE_ID = 1

Source = Literal["command", "pause", "system", "button"]


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


async def record_change(
    session: AsyncSession,
    *,
    field: str,
    old_value: str | None,
    new_value: str | None,
    source: Source,
) -> None:
    """Write a bare audit row for a change outside `user_state`.

    Commits. Used by 2b's /forget (plan section 11); update_state()
    remains the only way to change a user_state field.

    Callers must pass identifiers, never content -- see the module
    docstring.
    """
    session.add(
        StateChange(field=field, old_value=old_value, new_value=new_value, source=source)
    )
    await session.commit()
