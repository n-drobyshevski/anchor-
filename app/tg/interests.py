"""`/interests` and `/interests add <forums|guides|ref> <тема>` (Phase 6
plan section 7; milestone 6d). The `it:x:<id>` callback is this module's
own [✖], mirroring `app/tg/orders.py`'s `so:x:<id>` [Снять].

Everything Telegram-shaped lives here; `app/core/interests.py` is the
domain layer and imports no aiogram types.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import interests
from app.db.models import InterestTopic
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

REMOVE = "✖"
STALE = "Устарело."

TOPICS_EMPTY = "Тем нет."
ADD_USAGE = (
    "Что искать? Напиши так: /interests add forums бессонница "
    "(пакеты: forums, guides, ref)."
)
UNKNOWN_PACKET_REPLY = "Пакеты: forums, guides, ref."
TOPIC_TOO_LONG = f"Слишком длинная тема — уложись в {interests.TEXT_MAX} символов."
TOPIC_ADDED = "Буду искать: «{text}» [{packet}]."

# app/core/interests.add_topic's refusal codes -- a closed set, same
# convention as app/tg/research.py's own *_REFUSALS dicts. "empty" is
# here only so this is a total map: run_add's own parsing already
# refuses a blank topic with ADD_USAGE before add_topic ever sees it.
ADD_REFUSALS = {
    "unknown_packet": UNKNOWN_PACKET_REPLY,
    "too_long": TOPIC_TOO_LONG,
    "refused": interests.REFUSAL_TEXT,
    "cap": interests.CAP_TEXT,
    "empty": ADD_USAGE,
}


def parse_add_args(raw: str | None) -> tuple[str, str] | None:
    """Split "<packet> <тема...>" into `(packet, topic)`, the same shape
    `app/tg/research.py`'s `parse_study_args` splits `/study`'s own
    tail. None means there is nothing usable: no args, a packet with
    nothing after it, or a topic that is only whitespace."""
    if not raw:
        return None
    parts = raw.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    topic = parts[1].strip()
    if not topic:
        return None
    return parts[0].lower(), topic


def render_topic_line(topic: InterestTopic) -> str:
    return f"#{topic.id} [{topic.packet}] {topic.text}"


def topics_keyboard(rows: list[InterestTopic]) -> InlineKeyboardMarkup | None:
    """One [✖] row per active topic -- `/orders`' own `_retire_keyboard`
    shape, one button per row rather than one row of many buttons, so a
    press unambiguously names one topic."""
    if not rows:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{REMOVE} «{row.text[:24]}»", callback_data=f"it:x:{row.id}"
                )
            ]
            for row in rows
        ]
    )


def render_topics_list(rows: list[InterestTopic]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not rows:
        return TOPICS_EMPTY, None
    text = "\n".join(render_topic_line(row) for row in rows)
    return text, topics_keyboard(rows)


async def run_list(sessionmaker, bot: Bot, *, chat_id: int) -> None:
    async with sessionmaker() as session:
        rows = await interests.active_topics(session)
    text, markup = render_topics_list(rows)
    await send_keyboard(bot, chat_id, text, markup)


async def run_add(sessionmaker, settings: Settings, *, packet: str, topic: str) -> str:
    """`/interests add <packet> <тема>`. Returns the reply text.

    `app/core/interests.add_topic` owns the actual check order (packet
    name, topic length, the risk screen, the cap -- its own docstring);
    this function only turns the result into Russian.
    """
    async with sessionmaker() as session:
        result = await interests.add_topic(session, settings, packet=packet, text=topic)
    if result != "ok":
        return ADD_REFUSALS.get(result, ADD_USAGE)
    return TOPIC_ADDED.format(text=topic.strip(), packet=packet.lower())


async def handle_remove_callback(
    sessionmaker,
    bot: Bot,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """`it:x:<id>` -- `/interests`' own [✖], re-rendering the list in
    place (`app/tg/orders.py`'s `handle_retire_callback` is the template
    this copies)."""
    await answer_callback(bot, callback_id)
    _, _, raw_id = data.split(":", 2)

    try:
        topic_id = int(raw_id)
    except ValueError:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        result = await interests.retire(session, topic_id)

    if result == "stale":
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        rows = await interests.active_topics(session)
    text, markup = render_topics_list(rows)
    await edit_keyboard(bot, chat_id, message_id, text, markup)


__all__ = [
    "ADD_REFUSALS",
    "ADD_USAGE",
    "STALE",
    "TOPICS_EMPTY",
    "TOPIC_ADDED",
    "TOPIC_TOO_LONG",
    "UNKNOWN_PACKET_REPLY",
    "handle_remove_callback",
    "parse_add_args",
    "render_topic_line",
    "render_topics_list",
    "run_add",
    "run_list",
    "topics_keyboard",
]
