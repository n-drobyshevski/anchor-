"""`/interests`, `/interests add <forums|guides|ref> <тема>` (Phase 6
plan section 7; milestone 6d) and `/interests run [номер]`. The
`it:x:<id>` callback is this module's own [✖], mirroring
`app/tg/orders.py`'s `so:x:<id>` [Снять].

Everything Telegram-shaped lives here; `app/core/interests.py` is the
domain layer and imports no aiogram types.

**`/interests run [номер]` researches one saved topic now.** Idle
research reaches these topics only after hours of silence and only
while /study's shared daily slot is free; this is the way to ask for
one by hand. `app/research/jobs.enqueue_interest` owns the whole of it
(which topic, the checks and their order, its own daily allowance,
`RESEARCH_MANUAL_PER_DAY`, the queued job); `run_now` here only turns
its answer into Russian, the same split `app/tg/research.py`'s
`run_study` has with `enqueue_study`. The job it queues is an ordinary
/study one, so the completion line («Готово: N карточек. /notes») is
the worker's, not this module's. No button runs a topic, on purpose:
a callback would need its own replay gate, and the command already has
the router's `_once`. The list says how to run one instead
(`LIST_HINT`), but only while research is on -- a hint pointing at a
command that answers «Исследования выключены.» would only mislead.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import interests
from app.core.clock import Clock
from app.db.models import InterestTopic
from app.research import jobs as research_jobs
from app.tg import research as research_ui
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

# --- /interests run [номер] -------------------------------------------------

# Under the list, and only when there is a list and research is on
# (render_topics_list's `research_on`).
LIST_HINT = "Искать сейчас: /interests run [номер]."

RUN_USAGE = (
    "Какую тему искать? Напиши так: /interests run или /interests run 3 "
    "(номер из /interests)."
)
RUN_ACCEPTED = "Ищу: «{text}» [{packet}]. Карточки появятся в /notes."
RUN_NO_TOPICS = "Тем нет. Добавь: /interests add forums бессонница."
RUN_TOPIC_MISSING = "Нет такой темы."
# Not /study's QUOTA_EXHAUSTED: this is a separate allowance
# (RESEARCH_MANUAL_PER_DAY), and the reply should not suggest /study is
# spent too when it is not.
RUN_QUOTA_EXHAUSTED = "На сегодня лимит ручного поиска исчерпан."

# app/research/jobs.enqueue_interest's refusal codes. EMPTY_PACKET is
# not here, for the same reason it is not in app/tg/research.py's
# STUDY_REFUSALS: it needs the packet name, which run_now takes from
# the refusal itself and formats into research_ui.EMPTY_PACKET_TEMPLATE.
# DISABLED and CAP are /study's own replies, since they mean exactly
# what they mean there.
RUN_REFUSALS = {
    research_jobs.DISABLED: research_ui.DISABLED,
    research_jobs.NO_TOPIC: RUN_NO_TOPICS,
    research_jobs.TOPIC_GONE: RUN_TOPIC_MISSING,
    research_jobs.QUOTA: RUN_QUOTA_EXHAUSTED,
    research_jobs.CAP: research_ui.STUDY_REFUSALS[research_jobs.CAP],
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


_BIGINT_MAX = 2**63 - 1


def parse_run_args(raw: str | None) -> tuple[bool, int | None]:
    """`/interests run`'s tail: nothing, or one topic number as the list
    prints it (`3` or `#3`).

    Returns `(ok, topic_id)`: `(True, None)` for a bare `/interests run`
    (the rotation picks), `(True, n)` for a number, and `(False, None)`
    for anything else -- zero, a number too large for a bigint id, a
    word, a second token -- so the router answers RUN_USAGE rather than
    guess. ASCII digits only: `str.isdigit` alone also passes superscripts
    that `int()` then rejects. The upper bound is `interest_topic.id`'s
    own (BIGINT): a pasted phone or card number past it would otherwise
    reach `session.get` and make asyncpg raise a DataError after `_once`,
    so the update is retried and never answered.
    """
    tail = (raw or "").strip()
    if not tail:
        return True, None
    tail = tail.removeprefix("#")
    if not (tail.isascii() and tail.isdigit()):
        return False, None
    topic_id = int(tail)
    if not 0 < topic_id <= _BIGINT_MAX:
        return False, None
    return True, topic_id


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


def render_topics_list(
    rows: list[InterestTopic], *, research_on: bool
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The list's text and its [✖] rows. `research_on` is
    `settings.RESEARCH_ENABLED`, passed in rather than read here so this
    stays a pure render: with it, a non-empty list ends in LIST_HINT."""
    if not rows:
        return TOPICS_EMPTY, None
    text = "\n".join(render_topic_line(row) for row in rows)
    if research_on:
        text = f"{text}\n\n{LIST_HINT}"
    return text, topics_keyboard(rows)


async def run_list(sessionmaker, bot: Bot, *, chat_id: int, research_on: bool) -> None:
    async with sessionmaker() as session:
        rows = await interests.active_topics(session)
    text, markup = render_topics_list(rows, research_on=research_on)
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


async def run_now(
    sessionmaker,
    settings: Settings,
    clock: Clock,
    *,
    timezone: str,
    topic_id: int | None,
) -> str:
    """`/interests run [номер]`. Returns the reply text; the caller sends it.

    One call into `app/research/jobs.enqueue_interest`, which owns the
    topic choice and the check order (disabled, the topic, the manual
    quota, the cap -- its own docstring); this only turns the result
    into Russian. The accepted reply names the topic it is searching,
    which matters most for a bare `/interests run`, where the user did
    not say which one.
    """
    async with sessionmaker() as session:
        result = await research_jobs.enqueue_interest(
            session, settings, clock, timezone=timezone, topic_id=topic_id
        )
    if result.refusal == research_jobs.EMPTY_PACKET:
        return research_ui.EMPTY_PACKET_TEMPLATE.format(packet=result.packet)
    if result.refusal is not None:
        return RUN_REFUSALS.get(result.refusal, research_ui.DISABLED)
    return RUN_ACCEPTED.format(text=result.text, packet=result.packet)


async def handle_remove_callback(
    sessionmaker,
    bot: Bot,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
    research_on: bool,
) -> None:
    """`it:x:<id>` -- `/interests`' own [✖], re-rendering the list in
    place (`app/tg/orders.py`'s `handle_retire_callback` is the template
    this copies). `research_on` is `render_topics_list`'s, so the
    re-rendered list keeps (or keeps out) the same LIST_HINT the
    command's own list had."""
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
    text, markup = render_topics_list(rows, research_on=research_on)
    await edit_keyboard(bot, chat_id, message_id, text, markup)


__all__ = [
    "ADD_REFUSALS",
    "ADD_USAGE",
    "LIST_HINT",
    "RUN_ACCEPTED",
    "RUN_NO_TOPICS",
    "RUN_QUOTA_EXHAUSTED",
    "RUN_REFUSALS",
    "RUN_TOPIC_MISSING",
    "RUN_USAGE",
    "STALE",
    "TOPICS_EMPTY",
    "TOPIC_ADDED",
    "TOPIC_TOO_LONG",
    "UNKNOWN_PACKET_REPLY",
    "handle_remove_callback",
    "parse_add_args",
    "parse_run_args",
    "render_topic_line",
    "render_topics_list",
    "run_add",
    "run_list",
    "run_now",
    "topics_keyboard",
]
