"""Read/update the singleton `user_state` row, with an audit trail.

Every `user_state` field write goes through update_state() so a
`state_change` row (field, old_value, new_value, source) is always
recorded in the same transaction as the change itself (plan section 5).

2b adds record_change() for the other kind of audit row: a change worth
recording that is *not* a user_state field at all, such as a deleted
memory. Those cannot go through update_state(), which reads and writes
an attribute on the singleton row. The audit table accommodates them
because every one of its columns is nullable.

`source` gains `button` in 2b (an inline keyboard press) and
`extractor` in 2c, joining 1b's commands, 1d's pause words, and
startup/system-driven changes. `extractor` may only ever appear on a
memory or journal write -- never on a user_state field, which is the
invariant app/core/extract.py exists to keep (plan section 13).

3a adds set_counters() -- the one sanctioned way to write a
`user_state` field *without* an audit row. See its docstring for why
the exception exists and what keeps it from widening.

**Never put content in an audit row.** `old_value` is exactly where a
future maintainer would helpfully record a deleted memory's text, and
plan section 11 explicitly requires the opposite: "`state_change`
records `memory <id> deleted` with no text". tests/test_memory.py
asserts it.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StateChange, UserState

STATE_ID = 1

# 8b: "vault" (phase-8 plan section 6), only ever on field="memory" rows.
Source = Literal["command", "pause", "system", "button", "extractor", "welfare", "vault"]

# The only fields set_counters() may write (phase-3 plan section 4).
# An explicit allow-list rather than a denylist: a new sensitive column
# added later is excluded by default, which is the direction an
# accident should fail in. tests/test_outbound_counters.py asserts that
# persona_active, intensity, focus_on, due_action and streak are not in
# here -- the Phase 2 invariant that no automated path may write them
# (plan section 13) has to survive a module that writes user_state on
# every single inbound update.
COUNTER_FIELDS = frozenset(
    {"last_user_msg_at", "last_outbound_at", "ignored_in_row", "welfare_at"}
)


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


async def set_counters(session: AsyncSession, **values: Any) -> UserState:
    """Write one or more bookkeeping counters, with no state_change row.

    The deliberate exception to "every user_state write goes through
    update_state". These four columns are not decisions, they are
    traffic bookkeeping: `last_user_msg_at` and `ignored_in_row` change
    on *every* inbound update, and auditing them would turn
    `state_change` -- a short, readable log of things that were chosen
    -- into a message-rate counter that buries the rows a human
    actually wants to read, at two extra inserts per message on the
    reply path.

    What keeps the exception from widening is COUNTER_FIELDS: anything
    outside it raises, so this function structurally cannot become a
    back door to persona_active or intensity.

    Values may be SQL expressions (e.g. `UserState.ignored_in_row + 1`)
    so an increment stays a single atomic UPDATE rather than a
    read-modify-write.
    """
    unknown = sorted(set(values) - COUNTER_FIELDS)
    if unknown:
        raise ValueError(
            "set_counters may only write "
            + ", ".join(sorted(COUNTER_FIELDS))
            + f"; refused: {', '.join(unknown)}. A field that is a decision "
            "rather than a counter belongs in update_state(), which audits it."
        )
    if not values:
        return await get_state(session)

    await session.execute(
        update(UserState).where(UserState.id == STATE_ID).values(**values)
    )
    await session.commit()
    state = await get_state(session)
    await session.refresh(state)
    return state
