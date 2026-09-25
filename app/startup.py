"""One-time startup work, run after migrations in both webhook and polling
modes (plan section 5, last line / section 17 milestone 1b).

1. Upsert the singleton user_state row from config (ALLOWED_CHAT_ID,
   TZ_DEFAULT). Safe to run on every boot: chat_id/timezone are
   refreshed from config, but persona_active/intensity are left alone
   on conflict, since they are runtime state (changed by commands and
   pause words), not config.
2. Hash persona.md and insert a persona_version row only if that hash
   is new, so editing the file produces exactly one new row per edit.
   The read+hash itself lives in app/core/prompt.py's load_persona(),
   the same helper core/turn.py uses to build the system prompt -- one
   read path for persona content, shared here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.prompt import PERSONA_PATH, load_persona, persona_path_for
from app.core.state import STATE_ID
from app.db.models import PersonaVersion, UserState

logger = logging.getLogger(__name__)

DEFAULT_PERSONA_PATH = PERSONA_PATH


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
    body, sha256 = load_persona(persona_path)

    existing = await session.execute(
        select(PersonaVersion.id).where(PersonaVersion.sha256 == sha256)
    )
    if existing.scalar_one_or_none() is not None:
        return False

    session.add(PersonaVersion(sha256=sha256, body=body))
    await session.commit()
    return True


def warn_partial_backup_config(settings: Settings) -> list[str]:
    """Warn once when some, but not all, backup settings are set.

    Never raises: a half-configured backup must not keep the bot from
    starting. The nightly job records not_configured for it, exactly as
    for an empty config (app/ops/backup.py). Logs setting *names* only.
    Returns the missing names, for tests.
    """
    from app.ops.backup import missing_config

    missing = missing_config(settings)
    if missing and len(missing) < 5:
        logger.warning(
            "backup partially configured",
            extra={"event": "backup", "error_code": "partial_config", "fields": ",".join(missing)},
        )
    return missing


async def run_startup_tasks(
    session: AsyncSession, settings: Settings, persona_path: Path | None = None
) -> None:
    """Run both startup tasks in the order plan section 5 specifies.

    `persona_path` defaults to the file `settings.PERSONA_FILE` names, so
    the hash recorded here is the hash of the persona actually served.
    """
    await upsert_user_state(session, settings.ALLOWED_CHAT_ID, settings.TZ_DEFAULT)
    warn_partial_backup_config(settings)
    await sync_persona_version(session, persona_path or persona_path_for(settings))
