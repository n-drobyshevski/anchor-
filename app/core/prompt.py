"""Persona loading and prompt assembly (plan section 9).

`load_persona()` and `PERSONA_PATH` live here, not in app/startup.py,
so there is exactly one read path for persona.md; app/startup.py's
sync_persona_version() calls load_persona() internally instead of
hashing the file itself, without changing its own signature (tests/
test_startup.py calls it directly).

Prompt order (2b: plan section 7, which amends Phase 1 section 9),
stable prefix first for cache-friendliness in general (Cydonia, the
current model, has no implicit caching -- see app/llm/openrouter.py --
but the ordering costs nothing and keeps the door open for a future
model that does cache):
  1. system  -- persona.md body, byte-identical every call.
  2. system  -- "## Что ты знаешь (закреплено)": pinned memories.
     Changes only when pins change.
  3. system  -- "## Прошлые сессии": the last 3 closed scenes'
     summaries, oldest first. Changes only when a scene closes.
  4. transcript -- last TRANSCRIPT_TURNS `message` rows with ooc=false
     and kind in (chat, checkin), oldest first, excluding the row(s)
     belonging to the current update_id (see the double-user-message
     bug this guards against, below).
  5. system  -- the "## Сейчас" block, rebuilt every turn: local time,
     Russian weekday, intensity, "## Может быть важно" with the
     retrieved memories, and any flags.
  6. user    -- the new text.

Sections 2 and 3 are **omitted entirely when empty** rather than
emitted as a bare header. An empty heading is noise to the model, and
it would churn the byte-stable prefix that the ordering above exists to
protect.

**This module never sees a memory id.** `build_messages` takes pinned
and retrieved memories as `list[str]`, not as ORM rows, which makes
plan section 7's "memory IDs are never shown to the chat model; only
the extractor sees IDs" structurally impossible to violate rather than
a property of an f-string somewhere. Retrieval itself lives in
app/core/memory.py and is driven by app/core/turn.py, which needs the
ids back anyway to mark them used after delivery.

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

# Plan section 7, verbatim. Deliberately minimal -- no persona, no
# "## Сейчас" block -- unlike build_messages()'s byte-stable prefix,
# there is nothing here that needs to be cache-friendly, since neutral
# mode is meant to be rare and short-lived.
# Plan section 7 item 4: the persona transcript sees chat and check-in
# rows only. Welfare turns (2e) and canned replies are excluded here by
# kind, independently of the ooc flag that also excludes them -- one
# filter failing must not be enough to leak a welfare exchange into the
# persona's context.
PERSONA_TRANSCRIPT_KINDS = ("chat", "checkin")

NEUTRAL_SYSTEM_PROMPT = (
    "Ты нейтральный ассистент. Роль Anchor сейчас выключена. Отвечай спокойно и по делу, "
    "на языке пользователя. Не возвращайся в роль; если спросят как вернуться — подскажи команду /in."
)

PINNED_HEADER = "## Что ты знаешь (закреплено)"
SESSIONS_HEADER = "## Прошлые сессии"
RETRIEVED_HEADER = "## Может быть важно"

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


def _bullets(header: str, items: list[str]) -> list[str]:
    """`header` followed by `- item` lines, or nothing at all when empty."""
    if not items:
        return []
    return [header, *(f"- {item}" for item in items)]


def _ago(moment: datetime.datetime, *, tz: ZoneInfo) -> str:
    """"сегодня" / "вчера" / "N дн. назад", for the now block."""
    days = (datetime.datetime.now(tz).date() - moment.astimezone(tz).date()).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{days} дн. назад"


def build_now_block(
    *,
    timezone: str,
    intensity: int,
    flags: list[str] | None = None,
    retrieved: list[str] | None = None,
    focus_on: bool = False,
    due_action: str | None = None,
    due_set_at: datetime.datetime | None = None,
    streak: int = 0,
    last_checkin_at: datetime.datetime | None = None,
) -> str:
    """The "## Сейчас" system message (plan section 7), rebuilt every turn.

    2b appends "## Может быть важно" with the retrieved memories. They
    belong *inside* this block rather than as their own message because
    they are the most volatile thing in the prompt and the ordering is
    cache-aware: everything that changes per-turn is last.

    2c filled in "Фокус" and "Главное действие"; 2d adds "Серия" and
    "Последний чек-ин", completing plan section 7's block.
    """
    now_local = datetime.datetime.now(ZoneInfo(timezone))
    weekday = _RU_WEEKDAYS[now_local.weekday()]
    lines = [
        "## Сейчас",
        f"Локальное время: {now_local.strftime('%Y-%m-%d %H:%M')} ({timezone}), {weekday}",
        f"Интенсивность: {intensity}/5 · Фокус: {'вкл' if focus_on else 'выкл'} "
        f"· Серия: {streak} дн.",
    ]
    tz = ZoneInfo(timezone)
    if last_checkin_at is not None:
        stamp = last_checkin_at.astimezone(tz).strftime("%H:%M")
        lines.append(f"Последний чек-ин: {_ago(last_checkin_at, tz=tz)} {stamp}")
    else:
        lines.append("Последний чек-ин: давно")
    if due_action:
        when = f" (задано {_ago(due_set_at, tz=tz)})" if due_set_at else ""
        lines.append(f"Главное действие: «{due_action}»{when}")
    else:
        lines.append("Главное действие: нет")
    lines.extend(_bullets(RETRIEVED_HEADER, retrieved or []))
    lines.extend(flags or [])
    return "\n".join(lines)


async def _load_transcript(
    session: AsyncSession,
    *,
    ooc: bool,
    update_id: int | None,
    limit: int,
    kinds: tuple[str, ...] | None = None,
) -> list[Message]:
    """The last `limit` message rows with `ooc=ooc`, oldest first, excluding `update_id`.

    Shared by build_messages() (ooc=False, the persona transcript) and
    build_neutral_messages() (ooc=True, plan section 7's neutral-mode
    context) -- one query, one exclusion rule. `is_distinct_from`, not
    `!=`: plain `!=` silently drops any historical row whose update_id
    is NULL (NULL != x is NULL/unknown in SQL, which excludes rather
    than includes it).

    `kinds` (2b) is a *parameter* rather than a filter baked in here,
    and is passed only by build_messages(). Plan section 7 restricts
    the persona transcript to kind in (chat, checkin); applying that to
    this shared helper unconditionally would also stop neutral mode
    from seeing kind='canned' rows, a silent behaviour change to 1d
    that no test would catch (tests/test_turn.py's ooc rows are written
    with the default kind='chat' and would keep passing).
    """
    stmt = (
        select(Message)
        .where(Message.ooc.is_(ooc))
        .where(Message.update_id.is_distinct_from(update_id))
        .order_by(Message.id.desc())
        .limit(limit)
    )
    if kinds is not None:
        stmt = stmt.where(Message.kind.in_(kinds))
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
    pinned: list[str] | None = None,
    summaries: list[str] | None = None,
    retrieved: list[str] | None = None,
    focus_on: bool = False,
    due_action: str | None = None,
    due_set_at: datetime.datetime | None = None,
    streak: int = 0,
    last_checkin_at: datetime.datetime | None = None,
    persona_path: Path = PERSONA_PATH,
) -> list[LLMMessage]:
    """Assemble the full message list for one turn, in plan section 7's order.

    `pinned`, `summaries` and `retrieved` are plain strings supplied by
    the caller (app/core/turn.py), never ORM rows and never ids -- see
    the module docstring. All three default to None, so every Phase 1
    call site keeps its exact previous behaviour: with no memories and
    no summaries the empty sections are omitted and the message list is
    identical to what section 9 produced.
    """
    persona_body, _ = load_persona(persona_path)
    transcript = await _load_transcript(
        session,
        ooc=False,
        update_id=update_id,
        limit=transcript_turns,
        kinds=PERSONA_TRANSCRIPT_KINDS,
    )

    messages = [LLMMessage(role="system", content=persona_body)]

    pinned_block = _bullets(PINNED_HEADER, pinned or [])
    if pinned_block:
        messages.append(LLMMessage(role="system", content="\n".join(pinned_block)))

    sessions_block = _bullets(SESSIONS_HEADER, summaries or [])
    if sessions_block:
        messages.append(LLMMessage(role="system", content="\n".join(sessions_block)))

    messages.extend(LLMMessage(role=row.role, content=row.content) for row in transcript)
    messages.append(
        LLMMessage(
            role="system",
            content=build_now_block(
                timezone=timezone,
                intensity=intensity,
                flags=flags,
                retrieved=retrieved,
                focus_on=focus_on,
                due_action=due_action,
                due_set_at=due_set_at,
                streak=streak,
                last_checkin_at=last_checkin_at,
            ),
        )
    )
    messages.append(LLMMessage(role="user", content=user_text))
    return messages


async def build_neutral_messages(
    session: AsyncSession,
    *,
    user_text: str,
    update_id: int | None,
    limit: int = 10,
) -> list[LLMMessage]:
    """Assemble the neutral-mode message list (plan section 7).

    Deliberately minimal: the neutral system prompt, then the last
    `limit` ooc=True rows (oldest first, excluding this update), then
    the user's text. No persona, no "## Сейчас" block. A separate
    function rather than a flag on build_messages(), which is built
    around a byte-stable persona prefix kept for cache-friendliness in
    general -- bending that function to sometimes drop the persona
    would fight that design instead of extending it.
    """
    transcript = await _load_transcript(session, ooc=True, update_id=update_id, limit=limit)

    messages = [LLMMessage(role="system", content=NEUTRAL_SYSTEM_PROMPT)]
    messages.extend(LLMMessage(role=row.role, content=row.content) for row in transcript)
    messages.append(LLMMessage(role="user", content=user_text))
    return messages
