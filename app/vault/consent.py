"""`/vault notes on|off`: the user's consent to notes being read (8e plan sections 5-6).

While `user_state.notes_consent` is false, nothing of either class is
indexed or retrieved. Turning it off deletes everything derived from
notes: every `role='note'` file row, which cascades to both chunk
tables through their composite keys. It is one transaction, so there
is never a moment with consent off and chunks still present. /delete
resets the flag too (purge.reset_values), so a wipe cannot be undone by
the next pass re-reading the same classified notes.

Neither direction needs a two-step confirm: `off` deletes only a
derived index, rebuildable from the vault, and `on` reads nothing the
user has not already classified.

**The one user_state column app/vault/ writes.** The package otherwise
changes no user_state field (tests/test_vault_isolation.py pins that
only this module writes UserState, and only this column). It uses a
targeted UPDATE, never update_state: consent is not a live control of
the persona, and it has exactly one writer.
"""

from __future__ import annotations

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserState, VaultFile


async def set_notes_consent(session: AsyncSession, on: bool) -> int:
    """Set consent and commit. Returns how many note file rows `off` deleted."""
    await session.execute(update(UserState).where(UserState.id == 1).values(notes_consent=on))
    deleted = 0
    if not on:
        result = await session.execute(delete(VaultFile).where(VaultFile.role == "note"))
        deleted = result.rowcount or 0
    await session.commit()
    return deleted
