"""Persona loading and prompt assembly (plan section 9).

`load_persona()` and `PERSONA_PATH` live here, not in app/startup.py,
so there is exactly one read path for persona.md; app/startup.py's
sync_persona_version() calls load_persona() internally instead of
hashing the file itself, without changing its own signature (tests/
test_startup.py calls it directly).

Prompt order, stable prefix first so xAI's prompt cache hits:
  1. system  -- persona.md body, byte-identical every call.
  2. transcript -- last TRANSCRIPT_TURNS `message` rows with ooc=false,
     oldest first, excluding the row(s) belonging to the current
     update_id (see the double-user-message bug this guards against,
     below).
  3. system  -- the "## Сейчас" block, rebuilt every turn: local time,
     Russian weekday, intensity, and any flags.
  4. user    -- the new text.

The double-user-message bug: core/turn.py stores the user's message
(step 1 of the turn) and then appends `user_text` again as the final
message (step 4 here). If the transcript query did not exclude the
current update_id, the model would see the same user message twice --
once from the transcript, once as the "new" user turn. `update_id.
is_distinct_from(update_id)` (rather than `!=`) is used so this also
behaves correctly for any historical row whose update_id happens to be
NULL, which plain `!=` would silently drop from the transcript
(NULL != x is NULL in SQL, which excludes the row, not includes it).
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message
from app.llm.provider import LLMMessage

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PERSONA_PATH = REPO_ROOT / "persona" / "persona.md"

# strftime("%A") depends on a ru_RU locale that is not installed in the
# container, so the weekday name is a hardcoded lookup instead
# (datetime.weekday(): Monday == 0).
_RU_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def load_persona(persona_path: Path = PERSONA_PATH) -> tuple[str, str]:
    """Read persona.md and return (body, sha256).

    The single read path for persona content, shared by app/startup.py
    (persona_version hashing) and build_messages() below (the system
    prompt) -- one place to change if the read ever needs to change
    (encoding, path, etc.), and byte-stability across calls falls out
    of both callers reading the same file rather than being asserted
    separately.
    """
    body = persona_path.read_text(encoding="utf-8")
    sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return body, sha256


def build_now_block(*, timezone: str, intensity: int, flags: list[str] | None = None) -> str:
    """The "## Сейчас" system message (plan section 9), rebuilt every turn."""
    now_local = datetime.datetime.now(ZoneInfo(timezone))
    weekday = _RU_WEEKDAYS[now_local.weekday()]
    lines = [
        "## Сейчас",
        f"Локальное время: {now_local.strftime('%Y-%m-%d %H:%M')} ({timezone}), {weekday}",
        f"Интенсивность: {intensity}/5",
    ]
    lines.extend(flags or [])
    return "\n".join(lines)


async def _load_transcript(
    session: AsyncSession, *, update_id: int | None, limit: int
) -> list[Message]:
    """The last `limit` non-OOC message rows, oldest first, excluding `update_id`."""
    stmt = (
        select(Message)
        .where(Message.ooc.is_(False))
        .where(Message.update_id.is_distinct_from(update_id))
        .order_by(Message.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    rows.reverse()
    return rows


async def build_messages(
    session: AsyncSession,
    *,
    timezone: str,
    intensity: int,
    user_text: str,
    update_id: int | None,
    transcript_turns: int,
    flags: list[str] | None = None,
    persona_path: Path = PERSONA_PATH,
) -> list[LLMMessage]:
    """Assemble the full message list for one turn, in plan section 9's order."""
    persona_body, _ = load_persona(persona_path)
    transcript = await _load_transcript(session, update_id=update_id, limit=transcript_turns)

    messages = [LLMMessage(role="system", content=persona_body)]
    messages.extend(LLMMessage(role=row.role, content=row.content) for row in transcript)
    messages.append(
        LLMMessage(
            role="system",
            content=build_now_block(timezone=timezone, intensity=intensity, flags=flags),
        )
    )
    messages.append(LLMMessage(role="user", content=user_text))
    return messages
