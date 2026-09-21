"""One-time startup work, run after migrations in both webhook and polling
modes (plan section 5, last line / section 17 milestone 1b).

1. Upsert the singleton user_state row from config (ALLOWED_CHAT_ID,
   TZ_DEFAULT). Safe to run on every boot: chat_id/timezone are
   refreshed from config, but persona_active/intensity are left alone
   on conflict, since they are runtime state (changed by commands and
   pause words), not config.
2. Hash persona.md and insert a persona_version row only if that hash
   is new, so editing the file produces exactly one new row per edit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.state import STATE_ID
from app.db.models import PersonaVersion, UserState

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PERSONA_PATH = REPO_ROOT / "persona" / "persona.md"


async def upsert_user_state(session: AsyncSession, chat_id: int, timezone: str) -> None:
    """Insert the id=1 row if missing, else refresh chat_id/timezone in place."""
    stmt = (
        pg_insert(UserState)
        .values(id=STATE_ID, chat_id=chat_id, timezone=timezone)
        .on_conflict_do_update(
            index_elements=[UserState.id],
            set_={"chat_id": chat_id, "timezone": timezone},
        )
    )
    await session.execute(stmt)
    await session.commit()


async def sync_persona_version(
    session: AsyncSession, persona_path: Path = DEFAULT_PERSONA_PATH
) -> bool:
    """Insert a new persona_version row iff persona.md's sha256 is new.

    Returns True iff a row was inserted.
    """
    body = persona_path.read_text(encoding="utf-8")
    sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()

    existing = await session.execute(
        select(PersonaVersion.id).where(PersonaVersion.sha256 == sha256)
    )
    if existing.scalar_one_or_none() is not None:
        return False

    session.add(PersonaVersion(sha256=sha256, body=body))
    await session.commit()
    return True


async def run_startup_tasks(
    session: AsyncSession, settings: Settings, persona_path: Path = DEFAULT_PERSONA_PATH
) -> None:
    """Run both startup tasks in the order plan section 5 specifies."""
    await upsert_user_state(session, settings.ALLOWED_CHAT_ID, settings.TZ_DEFAULT)
    await sync_persona_version(session, persona_path)
