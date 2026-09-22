"""Deleting everything (plan section 11, build rule 7: "/delete must really delete").

One transaction, one TRUNCATE, then a reset of the singleton state row.

**Why TRUNCATE without CASCADE.** The schema has exactly three foreign
keys (message -> telegram_update, message -> scene, memory -> memory),
none of them with ondelete=, and nothing outside PURGED_TABLES points
into it. So one TRUNCATE over the whole list succeeds. Leaving CASCADE
off is deliberate: if a future table ever references a purged one and is
not itself listed here, the statement fails loudly instead of silently
skipping it -- which is the direction rule 7 wants. A delete that
quietly misses a table is the worst outcome available.

RESTART IDENTITY is not only tidiness. After "delete everything",
/memories showing `#47` would be a lie about what is in there.

**The two invariants below are the real safety property**, and they are
asserted in tests/test_delete.py rather than here: every table is either
purged or explicitly kept, and every user_state column is either
preserved or explicitly reset. Both fail the moment somebody adds a
table or a column and forgets about this file, which is the only moment
anyone would notice.
"""

from __future__ import annotations

import logging

from sqlalchemy import text as sql_text, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.config import Settings
from app.core.state import STATE_ID, record_change
from app.db.models import UserState

logger = logging.getLogger(__name__)

# Plan section 11's delete list, plus pending_memory. Section 11 omits
# that one, and it belongs: it holds text the user typed at /remember
# and never classified. Leaving it behind after "delete all my data" is
# exactly the bug rule 7 names.
PURGED_TABLES = (
    "message",
    "memory",
    "scene",
    "checkin",
    "proposal",
    "journal",
    "state_change",
    "spend_ledger",
    "job",
    "telegram_update",
    "pending_memory",
    # 3a: every proactive message ever planned, sent, skipped or
    # cancelled. Purged, not kept: it is a record of what the bot said
    # to this user and when, which is exactly what "delete all my data"
    # means. It also FKs to `message`, so leaving it out made Postgres
    # refuse the whole TRUNCATE -- /delete failed outright rather than
    # partially succeeding.
    "outbound",
)

# user_state is reset in place, never dropped. persona_version is a
# hash of a file in this repo, not user data, and startup rebuilds it
# anyway (plan section 11: "Keep persona_version").
KEPT_TABLES = ("user_state", "persona_version")

# Columns that survive a wipe. Everything else on user_state must appear
# in reset_values() below.
PRESERVED_STATE_COLUMNS = ("id", "chat_id")


def reset_values(settings: Settings, clock: Clock) -> dict:
    """Every user_state column except the preserved two, at its default.

    Reset explicitly rather than left to the next boot: startup's
    upsert_user_state only refreshes chat_id and timezone on conflict,
    by explicit design, so nothing else would ever come back to its
    default on its own.

    `updated_at` is in here because the column has a server_default but
    no onupdate -- without it an in-place reset would leave the row
    claiming it was last touched when the bot first booted.
    """
    return {
        "persona_active": True,
        "intensity": 3,
        "timezone": settings.TZ_DEFAULT,
        "focus_on": False,
        "focus_since": None,
        "due_action": None,
        "due_set_at": None,
        "streak": 0,
        "last_checkin_at": None,
        "awaiting": None,
        "awaiting_ref": None,
        # 3a: the outbound counters (phase-3 plan section 4). A
        # /delete that left ignored_in_row at 3 would leave the bot
        # silent after a wipe that is meant to return it to factory
        # state, and a stale quiet_until would keep it muted.
        "quiet_until": None,
        "last_user_msg_at": None,
        "last_outbound_at": None,
        "ignored_in_row": 0,
        "welfare_at": None,
        "updated_at": clock.now_utc(),
    }


async def delete_everything(
    session: AsyncSession, settings: Settings, clock: Clock
) -> None:
    """Wipe every content table and reset user_state, in one transaction.

    user_state is updated, never deleted and reinserted: get_state()
    raises NoResultFound on a missing row, so any window without it
    would crash every message until the next restart.

    One state_change row is written afterwards, outside the wipe. The
    reset changes six state fields, and this would otherwise be the only
    state mutation in the codebase with no audit behind it. It carries
    the fact and the time and no content whatsoever -- so an /export run
    straight after a delete shows exactly one row, which is mildly
    surprising and more honest than a log with a hole in it.
    """
    await session.execute(
        sql_text(f"TRUNCATE TABLE {', '.join(PURGED_TABLES)} RESTART IDENTITY")
    )
    await session.execute(
        sql_update(UserState).where(UserState.id == STATE_ID).values(**reset_values(settings, clock))
    )
    await session.commit()

    await record_change(
        session, field="data", old_value=None, new_value="deleted", source="command"
    )
    logger.info("all user data deleted", extra={"event": "delete"})
