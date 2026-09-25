"""The memory commands and their inline keyboards (plan section 11).

Everything Telegram-shaped about memory lives here; app/core/memory.py
is the domain layer and imports no aiogram types.

**Idempotency.** The worker re-runs an update after any crash between
feed_update and complete(), and after the 60s stuck sweep
(app/worker.py). So a command that mutates must check first, exactly as
app/core/turn.py's run_hard_pause does: `_get_assistant_row(update_id)`
returning a row means this update has already been handled, and the
mutation must not run a second time. Without that gate a replayed
/remember would park a second pending row and send a second keyboard,
and a replayed /forget would write a second audit row.

The kind-button callback needs no such gate: it consumes its
pending_memory row with a DELETE ... RETURNING, so a second press finds
nothing and is answered "Устарело". Paging needs none either, being an
idempotent edit.

**Callbacks never write to `message`.** A callback carries no user
text, `Message.content` is NOT NULL, and writing "m:p:2" into the
transcript would put machine tokens in front of the model. Command
*replies* do get stored, as kind='canned', ooc=True -- which is what
buys them the idempotency above, and which plan section 7's transcript
filter already excludes from the persona's context.

Callback data format mirrors plan section 8's `p:a:<id>`:
    m:k:<kind>:<pending_id>   a kind button under /remember
    m:p:<offset>              a paging arrow under /memories
Kinds travel as their canonical English values, matching the check
constraint directly, and every form stays far inside Telegram's 64-byte
callback_data limit.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import memory
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

logger = logging.getLogger(__name__)

PAGE_SIZE = 20
LIST_TEXT_TRIM = 80

# Plan section 11's four offered kinds. `technique` exists in the schema
# but is not offered here: it is an adopt/extractor kind, not something
# the user classifies by hand.
KIND_LABELS = (
    ("identity", "Обо мне"),
    ("preference", "Предпочтение"),
    ("rule", "Правило"),
    ("event", "Событие"),
)
KIND_LABEL_BY_VALUE = dict(KIND_LABELS)

REMEMBER_USAGE = "Что запомнить? Напиши так: /remember я живу в Лилле."
REMEMBER_PROMPT = "Что это? «{text}»"
REMEMBER_SAVED = "Запомнил. #{id} [{kind}] {text}"
REMEMBER_DUPLICATE = "Уже знаю что-то очень похожее — не дублирую."
STALE = "Устарело."

MEMORIES_EMPTY = "Пока ничего не помню."
MEMORIES_HEADER = "Помню {total}:"

FORGET_USAGE = "Что забыть? Напиши так: /forget 12."
FORGET_DONE = "Забыл #{id}."
FORGET_MISSING = "Нет такой записи."
# W3 finding: forgetting the head of a chain a StudyCard still points at
# (an adopted technique, or the merged/corrected row of one) would
# either violate memory's own FK or, relinked to NULL, ck_study_card_
# adopted_has_memory -- app.core.memory.forget refuses it outright.
FORGET_PROTECTED = "Эта запись — часть принятой техники, так её не забыть."

PIN_USAGE = "Что закрепить? Напиши так: /pin 12."
UNPIN_USAGE = "Что открепить? Напиши так: /unpin 12."
PIN_DONE = "Закрепил #{id}."
UNPIN_DONE = "Открепил #{id}."
PIN_MISSING = "Нет такой записи."
PIN_OVER_CAP = "Закреплено уже {max}. Открепи что-нибудь сначала."

# Moved to app/core/memory.py (W3): both transports share one cap.
MEMORY_TEXT_MAX = memory.MEMORY_TEXT_MAX
TOO_LONG = "Слишком длинно — максимум {max} символов."


def kind_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=f"m:k:{value}:{pending_id}")
                for value, label in KIND_LABELS[:2]
            ],
            [
                InlineKeyboardButton(text=label, callback_data=f"m:k:{value}:{pending_id}")
                for value, label in KIND_LABELS[2:]
            ],
        ]
    )


def page_keyboard(offset: int, total: int) -> InlineKeyboardMarkup | None:
    """`‹ ›` arrows, each omitted at its own boundary.

    Omitting rather than disabling is half of why paging cannot error:
    an arrow that would re-render the same page is simply not there.
    edit_keyboard swallowing "message is not modified" is the other half,
    for the replayed-callback case that no amount of button-hiding can
    prevent.
    """
    buttons = []
    if offset > 0:
        buttons.append(
            InlineKeyboardButton(text="‹", callback_data=f"m:p:{max(0, offset - PAGE_SIZE)}")
        )
    if offset + PAGE_SIZE < total:
        buttons.append(InlineKeyboardButton(text="›", callback_data=f"m:p:{offset + PAGE_SIZE}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None


def render_page(rows, total: int, offset: int) -> str:
    """`#id [kind] 📌? text` per plan section 11."""
    if total == 0:
        return MEMORIES_EMPTY
    lines = [MEMORIES_HEADER.format(total=total)]
    for row in rows:
        text = row.text if len(row.text) <= LIST_TEXT_TRIM else row.text[: LIST_TEXT_TRIM - 1] + "…"
        pin = " 📌" if row.pinned else ""
        lines.append(f"#{row.id} [{row.kind}]{pin} {text}")
    if total > PAGE_SIZE:
        last = min(offset + PAGE_SIZE, total)
        lines.append(f"({offset + 1}–{last} из {total})")
    return "\n".join(lines)


async def show_memories_page(
    session, bot: Bot, chat_id: int, offset: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    rows, total = await memory.list_active(session, offset=offset, limit=PAGE_SIZE)
    return render_page(rows, total, offset), page_keyboard(offset, total)


# --- callbacks ---


async def handle_kind_callback(
    sessionmaker, bot: Bot, *, callback_id: str, chat_id: int, message_id: int, data: str
) -> None:
    """`m:k:<kind>:<pending_id>` -- the user picked a kind for /remember.

    Idempotent through take_pending's DELETE ... RETURNING: whichever
    press consumes the row wins, and any later one gets "Устарело".
    """
    _, _, kind, raw_id = data.split(":", 3)
    await answer_callback(bot, callback_id)

    try:
        pending_id = int(raw_id)
    except ValueError:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        text = await memory.take_pending(session, pending_id)

    if text is None or kind not in KIND_LABEL_BY_VALUE:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        written = await memory.write_memory(session, kind=kind, text=text, source="user")

    if written is None:
        await edit_keyboard(bot, chat_id, message_id, REMEMBER_DUPLICATE, None)
        return

    await edit_keyboard(
        bot,
        chat_id,
        message_id,
        REMEMBER_SAVED.format(id=written.id, kind=kind, text=written.text),
        None,
    )


async def handle_page_callback(
    sessionmaker, bot: Bot, *, callback_id: str, chat_id: int, message_id: int, data: str
) -> None:
    """`m:p:<offset>` -- a /memories paging arrow."""
    _, _, raw_offset = data.split(":", 2)
    await answer_callback(bot, callback_id)

    try:
        offset = max(0, int(raw_offset))
    except ValueError:
        return

    async with sessionmaker() as session:
        text, markup = await show_memories_page(session, bot, chat_id, offset)
    await edit_keyboard(bot, chat_id, message_id, text, markup)


# --- commands ---


async def run_remember(sessionmaker, bot: Bot, *, chat_id: int, text: str) -> None:
    """Park the text and offer the kind buttons. Caller handles the replay gate."""
    async with sessionmaker() as session:
        pending = await memory.add_pending(session, text)
    await send_keyboard(
        bot, chat_id, REMEMBER_PROMPT.format(text=text), kind_keyboard(pending.id)
    )


async def run_memories(sessionmaker, bot: Bot, *, chat_id: int) -> None:
    async with sessionmaker() as session:
        text, markup = await show_memories_page(session, bot, chat_id, 0)
    await send_keyboard(bot, chat_id, text, markup)


async def run_forget(sessionmaker, bot: Bot, *, chat_id: int, memory_id: int) -> str:
    """Hard-delete a memory. Returns the reply text.

    The audit row deliberately carries the id and nothing else -- plan
    section 11: "`state_change` records `memory <id> deleted` with no
    text". W3: the delete-plus-audit logic itself now lives in
    `memory.forget` (source="command", matching this handler's own
    audit source before the extraction), shared with the web panel;
    this function keeps only the reply text.
    """
    async with sessionmaker() as session:
        outcome = await memory.forget(session, memory_id, source="command")
    if outcome == memory.FORGET_PROTECTED:
        return FORGET_PROTECTED
    return FORGET_DONE.format(id=memory_id) if outcome == memory.FORGET_OK else FORGET_MISSING


async def run_set_pinned(
    sessionmaker, settings: Settings, *, memory_id: int, pinned: bool
) -> str:
    """Pin or unpin. Returns the reply text.

    Pinning refuses past MEMORY_PINNED_MAX rather than accepting
    silently: plan section 7 caps the *render* at that number, so a
    ninth pin would quietly stop reaching the model -- the worst
    possible outcome for a memory the user explicitly asked to always be
    remembered. W3: the cap check and the write itself now live in
    `memory.set_pinned_capped`, shared with the web panel; this function
    keeps only the reply text.
    """
    async with sessionmaker() as session:
        outcome = await memory.set_pinned_capped(
            session, memory_id, pinned, max_pinned=settings.MEMORY_PINNED_MAX
        )
    if outcome == memory.PIN_MISSING:
        return PIN_MISSING
    if outcome == memory.PIN_OVER_CAP:
        return PIN_OVER_CAP.format(max=settings.MEMORY_PINNED_MAX)
    return (PIN_DONE if pinned else UNPIN_DONE).format(id=memory_id)


def parse_id(raw: str | None) -> int | None:
    """`#12` and `12` both parse; anything else is None."""
    if not raw:
        return None
    candidate = raw.strip().lstrip("#")
    try:
        return int(candidate)
    except ValueError:
        return None
