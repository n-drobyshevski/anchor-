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

5a (phase-5 plan section 10) inserts four more stable-prefix sections
between persona and the transcript -- "## Поправки", "## Голос",
"## Договорённости" and "## Твои заметки" -- and extends the "## Сейчас"
block with a mood line, a nickname directive, and 5c/5e placeholders.
See build_messages()'s and build_now_block()'s own docstrings for the
exact order; every new section follows the same "omitted when empty"
rule sections 2 and 3 above already establish.

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

from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.mood import GLOSS
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
# 3b: 'outbound' joins these (phase-3 plan section 7: "Outbound
# messages **are** included in the persona transcript, so Anchor
# remembers what it said"). Without it the bot would re-send
# essentially the same morning message every day, having no memory of
# the previous one -- the transcript is the only thing that stops it.
PERSONA_TRANSCRIPT_KINDS = ("chat", "checkin", "outbound")

NEUTRAL_SYSTEM_PROMPT = (
    "Ты нейтральный ассистент. Роль Anchor сейчас выключена. Отвечай спокойно и по делу, "
    "на языке пользователя. Не возвращайся в роль; если спросят как вернуться — подскажи команду /in."
)

# 5a (phase-5 plan section 10). Stable-prefix sections, in the order
# they appear in build_messages() below: amendments and voice come
# right after persona (amendments amend it, voice colors it), orders
# and notebook come after pinned memories and before the session
# summaries -- all four still stable within a scene or longer, just
# less so than persona.md itself.
AMENDMENTS_HEADER = "## Поправки (одобрены тобой)"
VOICE_HEADER = "## Голос"
PINNED_HEADER = "## Что ты знаешь (закреплено)"
ORDERS_HEADER = "## Договорённости"
NOTEBOOK_HEADER = "## Твои заметки"
SESSIONS_HEADER = "## Прошлые сессии"
RETRIEVED_HEADER = "## Может быть важно"
# 5e placeholder (plan section 11a): not populated by any caller in 5a,
# but part of the "## Сейчас" block's fixed shape from here on so 5e
# only has to start passing `callback=`, not reorder anything.
CALLBACK_HEADER = "## Можно вспомнить (только если к месту)"

# 5b's labels for the three notebook kinds, in the fixed order plan
# section 6 renders them. Kept here (not in a later milestone's module)
# because the "## Твои заметки" line shape is part of this module's
# stable-section contract, exactly like _bullets() below.
_NOTEBOOK_LABELS = (
    ("intentions", "Намерения"),
    ("observations", "Наблюдения"),
    ("threads", "Незакрытое"),
)
# 4d, phase-4 plan section 10. Its own header, not folded into
# RETRIEVED_HEADER, because the two are different kinds of thing and
# the model should treat them differently: what is under "Может быть
# важно" is a fact the reply must stay consistent with, what is here is
# a method the reply may choose to use. The parenthetical is the point
# -- these arrived from the open web and are in this prompt only
# because the user read one and pressed [Принять].
TECHNIQUES_HEADER = "## Приёмы (одобрены тобой)"
# P2 (planner read path). Its own header, placed right after "Главное
# действие" and before the retrieved-memories section -- see
# build_now_block's docstring for why.
PLAN_HEADER = "## План на сегодня (из планера)"

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


def _notebook_lines(notebook: dict[str, list[str]] | None) -> list[str]:
    """"## Твои заметки" (plan section 6), or nothing when there is nothing to say.

    One line per non-empty kind, `; `-joined -- not `_bullets()`'s one
    line per item, because plan section 6's own example renders each
    kind as a single "Намерения: …" line rather than a bulleted list.
    A kind with no active entries is left out entirely, same convention
    as every other optional section in this module.
    """
    if not notebook:
        return []
    lines: list[str] = []
    for key, label in _NOTEBOOK_LABELS:
        items = notebook.get(key) or []
        if items:
            lines.append(f"{label}: " + "; ".join(items))
    if not lines:
        return []
    return [NOTEBOOK_HEADER, *lines]


def _ago(moment: datetime.datetime, *, today: datetime.date, tz: ZoneInfo) -> str:
    """"сегодня" / "вчера" / "N дн. назад", for the now block.

    `today` is passed in rather than read, so the whole block is a
    pure function of the clock the caller was given.
    """
    days = (today - moment.astimezone(tz).date()).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{days} дн. назад"


def build_now_block(
    *,
    clock: Clock,
    timezone: str,
    intensity: int,
    flags: list[str] | None = None,
    retrieved: list[str] | None = None,
    techniques: list[str] | None = None,
    focus_on: bool = False,
    due_action: str | None = None,
    due_set_at: datetime.datetime | None = None,
    streak: int = 0,
    last_checkin_at: datetime.datetime | None = None,
    mood: str | None = None,
    nickname_directive: str | None = None,
    orders_yesterday: str | None = None,
    callback: str | None = None,
    planner: list[str] | None = None,
) -> str:
    """The "## Сейчас" system message (plan section 7), rebuilt every turn.

    2b appends "## Может быть важно" with the retrieved memories. They
    belong *inside* this block rather than as their own message because
    they are the most volatile thing in the prompt and the ordering is
    cache-aware: everything that changes per-turn is last.

    2c filled in "Фокус" and "Главное действие"; 2d adds "Серия" and
    "Последний чек-ин", completing plan section 7's block.

    4d adds "## Приёмы (одобрены тобой)" -- adopted research cards, the
    only path by which anything read from the open web reaches this
    prompt, and only after the user pressed [Принять] on it (phase-4
    plan section 10).

    5a reorders this block to match phase-5 plan section 10 and adds
    three lines/sections: `mood` right after the intensity/focus/streak
    line (only when given -- neutral and welfare callers never pass
    one), `nickname_directive` after the due action and last check-in
    (also only when given), and `orders_yesterday`/`callback`
    placeholders for 5c/5e, wired the same way. **The due action now
    comes before the last check-in**, the opposite of Phase 1-4's
    order -- plan section 10 states it that way and no existing test
    encoded the old relative order (only substring checks), so nothing
    else needed updating for the swap.

    P2 adds `planner`, the rendered agenda lines from
    app/planner/snapshot.py's render_lines() -- never ORM rows, never a
    partner id, same discipline as `pinned`/`retrieved`/`techniques`
    above. `planner=None` (the default, and every call site predating
    P2) must produce byte-identical output to before this parameter
    existed: `_bullets` already returns `[]` for `None`, so the section
    is simply omitted, exactly like an empty `retrieved` list is.
    """
    now_local = clock_module.now_local(clock, timezone)
    weekday = _RU_WEEKDAYS[now_local.weekday()]
    lines = [
        "## Сейчас",
        f"Локальное время: {now_local.strftime('%Y-%m-%d %H:%M')} ({timezone}), {weekday}",
        f"Интенсивность: {intensity}/5 · Фокус: {'вкл' if focus_on else 'выкл'} "
        f"· Серия: {streak} дн.",
    ]
    if mood is not None:
        lines.append(f"Настроение: {mood} — {GLOSS[mood]}")
    tz = ZoneInfo(timezone)
    today = now_local.date()
    if due_action:
        when = f" (задано {_ago(due_set_at, today=today, tz=tz)})" if due_set_at else ""
        lines.append(f"Главное действие: «{due_action}»{when}")
    else:
        lines.append("Главное действие: нет")
    if last_checkin_at is not None:
        stamp = last_checkin_at.astimezone(tz).strftime("%H:%M")
        lines.append(
            f"Последний чек-ин: {_ago(last_checkin_at, today=today, tz=tz)} {stamp}"
        )
    else:
        lines.append("Последний чек-ин: давно")
    if orders_yesterday:
        lines.append(f"Договорённости вчера: {orders_yesterday}")
    if nickname_directive:
        lines.append(nickname_directive)
    lines.extend(_bullets(PLAN_HEADER, planner or []))
    lines.extend(_bullets(RETRIEVED_HEADER, retrieved or []))
    # After the retrieved memories and before the flags: a technique is
    # less volatile than what this turn happened to match, and the flags
    # stay last because they are the most volatile thing in the prompt.
    lines.extend(_bullets(TECHNIQUES_HEADER, techniques or []))
    if callback:
        lines.append(CALLBACK_HEADER)
        lines.append(f"- {callback}")
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
        .order_by(Message.id.desc())
        .limit(limit)
    )
    # 3b: only exclude when there is a current turn to exclude. A
    # proactive message (phase-3) has no update_id at all, and
    # `update_id IS DISTINCT FROM NULL` is *true* for every non-null
    # row -- so passing None used to mean "drop every outbound and
    # canned row", silently emptying the very transcript that stops
    # Anchor repeating yesterday's morning message.
    if update_id is not None:
        stmt = stmt.where(Message.update_id.is_distinct_from(update_id))
    if kinds is not None:
        stmt = stmt.where(Message.kind.in_(kinds))
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    rows.reverse()
    return rows


async def recent_transcript(session: AsyncSession, limit: int) -> list[Message]:
    """The last `limit` in-character messages, oldest first.

    3d: the tick decision (app/core/tick.py) needs the same view of the
    conversation the persona gets, and building a second query for it
    would be a second place to forget that welfare and canned rows are
    excluded. A thin wrapper rather than making _load_transcript public,
    because the two filters that matter -- ooc=False and
    PERSONA_TRANSCRIPT_KINDS -- are fixed here rather than left to the
    caller to get right.

    `update_id=None` means "exclude nothing", which is only correct
    because 3b made that exclusion conditional; before then it would
    have dropped every row whose update_id is NULL, outbound rows
    included.
    """
    return await _load_transcript(
        session,
        ooc=False,
        update_id=None,
        limit=limit,
        kinds=PERSONA_TRANSCRIPT_KINDS,
    )


async def build_messages(
    session: AsyncSession,
    *,
    clock: Clock,
    timezone: str,
    intensity: int,
    user_text: str,
    update_id: int | None,
    transcript_turns: int,
    flags: list[str] | None = None,
    pinned: list[str] | None = None,
    summaries: list[str] | None = None,
    retrieved: list[str] | None = None,
    techniques: list[str] | None = None,
    focus_on: bool = False,
    due_action: str | None = None,
    due_set_at: datetime.datetime | None = None,
    streak: int = 0,
    last_checkin_at: datetime.datetime | None = None,
    planner: list[str] | None = None,
    persona_path: Path = PERSONA_PATH,
    amendments: list[str] | None = None,
    voice_lines: list[str] | None = None,
    orders: list[str] | None = None,
    notebook: dict[str, list[str]] | None = None,
    mood: str | None = None,
    nickname_directive: str | None = None,
    orders_yesterday: str | None = None,
    callback: str | None = None,
) -> list[LLMMessage]:
    """Assemble the full message list for one turn, plan section 10's order.

    `pinned`, `summaries` and `retrieved` are plain strings supplied by
    the caller (app/core/turn.py), never ORM rows and never ids -- see
    the module docstring. All three default to None, so every Phase 1
    call site keeps its exact previous behaviour: with no memories and
    no summaries the empty sections are omitted and the message list is
    identical to what section 9 produced.

    5a adds `amendments`, `voice_lines`, `orders`, `notebook`, `mood`,
    `nickname_directive`, `orders_yesterday` and `callback` -- all
    keyword-only, all defaulting to empty, and every one of them
    producing an omitted section when empty, the same convention 2b's
    `pinned`/`summaries` already established. Only `voice_lines`,
    `mood` and `nickname_directive` are wired up by any caller in 5a
    (app/core/persona_context.py's `gather()`); `amendments`, `orders`,
    `notebook`, `orders_yesterday` and `callback` exist here already so
    milestones 5b-5e only have to start passing data, never touch this
    function's shape or the fixed order below.
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

    amendments_block = _bullets(AMENDMENTS_HEADER, amendments or [])
    if amendments_block:
        messages.append(LLMMessage(role="system", content="\n".join(amendments_block)))

    if voice_lines:
        voice_block = [VOICE_HEADER, *(f"- {line}" for line in voice_lines)]
        messages.append(LLMMessage(role="system", content="\n".join(voice_block)))

    pinned_block = _bullets(PINNED_HEADER, pinned or [])
    if pinned_block:
        messages.append(LLMMessage(role="system", content="\n".join(pinned_block)))

    orders_block = _bullets(ORDERS_HEADER, orders or [])
    if orders_block:
        messages.append(LLMMessage(role="system", content="\n".join(orders_block)))

    notebook_block = _notebook_lines(notebook)
    if notebook_block:
        messages.append(LLMMessage(role="system", content="\n".join(notebook_block)))

    sessions_block = _bullets(SESSIONS_HEADER, summaries or [])
    if sessions_block:
        messages.append(LLMMessage(role="system", content="\n".join(sessions_block)))

    messages.extend(LLMMessage(role=row.role, content=row.content) for row in transcript)
    messages.append(
        LLMMessage(
            role="system",
            content=build_now_block(
                clock=clock,
                timezone=timezone,
                intensity=intensity,
                flags=flags,
                retrieved=retrieved,
                techniques=techniques,
                focus_on=focus_on,
                due_action=due_action,
                due_set_at=due_set_at,
                streak=streak,
                last_checkin_at=last_checkin_at,
                mood=mood,
                nickname_directive=nickname_directive,
                orders_yesterday=orders_yesterday,
                callback=callback,
                planner=planner,
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
