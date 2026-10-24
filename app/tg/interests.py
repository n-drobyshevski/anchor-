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

**`/interests suggest` and its `is:` buttons** (anchor-interest-
suggestions-plan.md sections 2 and 5). The model proposes topics; only a
tap makes one. `app/research/suggest.py` owns the whole domain side --
the input, the call, validation, the batch and the tap's one
transaction (`decide`) -- and this module the Russian, the message and
the buttons, the same split as `run_now` above:

- `run_suggest` turns `enqueue_suggest`'s answer into a reply: «Подбираю
  темы. Пришлю, когда будут.», one of the refusals, or -- for a finished
  batch that never went out -- that batch's own message, sent now
  instead of a new one being paid for.
- `send_suggestions` is the one send path for a manual and a weekly
  batch alike. app/worker.py calls it after every INTEREST_SUGGEST job,
  after every idle run and on every heartbeat tick, so a batch held by
  quiet hours goes out once they end. It sends the newest finished,
  unsent batch while `RESEARCH_ENABLED` is on and `may_report_now`
  allows it -- an out-of-character report like a /read completion line:
  no Outbound row, no counters -- and expires the suggestions of a
  batch left unsent past `suggest.UNSENT_TTL`. The send comes first,
  then `mark_sent`: a crash between the two sends the message twice,
  and both copies' buttons then decide the same rows, so the second tap
  is stale. A lock keeps the heartbeat and the claim loop from sending
  the same batch at once.
- `send_batch_failed` is the one line a manual batch that failed sends
  in its place, so «Пришлю, когда будут.» is never followed by silence.
- `handle_suggestion_callback` is one tap, `is:<a|r|x>:<id>:<epoch>`:
  «N · добавить», «N · добавить и искать», «N · ✖». `decide` is one
  transaction (the conditional UPDATE on the epoch, then
  app/core/interests.py's own checks, whose refusal rolls the tap back
  and leaves the suggestion open); an `r` then runs the new topic
  through `jobs.enqueue_interest` after that commit, so a refused run
  never undoes the add and its reason is the toast. The message is then
  re-rendered by edit only: a decided row reads «N · ✓» or «N · ✖», an
  expired one goes.

Telegram only: the router's is_web_sink guards and app/web/ingress.py's
BLOCKED_CALLBACK_PREFIX both refuse `is:`, since the message's text can
be drawn from lens notes and a tap can start a paid search. Logs carry
batch ids, counts and codes, never a suggestion, a reason or a message
id.
"""

from __future__ import annotations

import asyncio
import logging
import re
import weakref

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import interests
from app.core.clock import Clock
from app.core.report import may_report_now
from app.core.state import get_state
from app.db.models import InterestSuggestBatch, InterestSuggestion, InterestTopic
from app.research import jobs as research_jobs
from app.research import suggest
from app.tg import research as research_ui
from app.tg.send import MESSAGE_LIMIT, answer_callback, edit_keyboard, send_keyboard

logger = logging.getLogger(__name__)

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
# The second line is `/interests suggest`'s (anchor-interest-suggestions-
# plan.md section 2), under the same condition: both commands answer
# «Исследования выключены.» while research is off.
SUGGEST_HINT = "Предложить темы: /interests suggest."
LIST_HINT = f"Искать сейчас: /interests run [номер].\n{SUGGEST_HINT}"

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


# --- /interests suggest ----------------------------------------------------

SUGGEST_ACCEPTED = "Подбираю темы. Пришлю, когда будут."
SUGGEST_NOTHING = "Не из чего предлагать: добавь тему через /interests add."
SUGGEST_NO_PACKET = "Ни один пакет поиска пока не настроен."
SUGGEST_QUOTA = "На сегодня предложений хватит."
SUGGEST_BUSY = "Уже подбираю."
# The race `run_suggest` can lose: between enqueue_suggest's answer and
# the send, the heartbeat's own pass sent the same batch.
SUGGEST_ALREADY_SENT = "Темы — в сообщении выше."
# A manual batch that failed after «Пришлю, когда будут.»
# (`send_batch_failed`): a provider error or a reply that did not parse.
SUGGEST_FAILED = "Не получилось подобрать темы. Попробуй позже."

# app/research/suggest.enqueue_suggest's refusal codes, a closed set.
# DISABLED and CAP are /study's own replies, as in RUN_REFUSALS.
SUGGEST_REFUSALS = {
    suggest.DISABLED: research_ui.DISABLED,
    suggest.NOTHING: SUGGEST_NOTHING,
    suggest.NO_PACKET: SUGGEST_NO_PACKET,
    suggest.BUSY: SUGGEST_BUSY,
    suggest.QUOTA: SUGGEST_QUOTA,
    suggest.CAP: research_ui.STUDY_REFUSALS[research_jobs.CAP],
}

# A failed manual batch's `error_code` -> the line `send_batch_failed`
# sends. The refusals met at run time say what they would have said at
# the command. Absent on purpose: INTERRUPTED (the next /interests
# suggest failed it, and got its own reply), PREEMPTED (the weekly
# body's only) and NOT_FOUND (/delete took it).
FAILED_LINES = {
    suggest.DISABLED: research_ui.DISABLED,
    suggest.NOTHING: SUGGEST_NOTHING,
    suggest.NO_PACKET: SUGGEST_NO_PACKET,
    suggest.CAP: research_ui.STUDY_REFUSALS[research_jobs.CAP],
    suggest.PROVIDER_ERROR: SUGGEST_FAILED,
    suggest.PARSE_FAIL: SUGGEST_FAILED,
}

SUGGESTIONS_HEADER = "Можно поискать:"
SUGGESTION_HEAD = "{n}. «{text}» [{packet}]"
SUGGESTION_LINE = SUGGESTION_HEAD + " — {why}"
SUGGESTIONS_NONE = "Новых тем не нашлось."
ELLIPSIS = "…"

ADD_BUTTON = "{n} · добавить"
ADD_AND_RUN_BUTTON = "{n} · добавить и искать"
DISMISS_BUTTON = "{n} · ✖"
ADDED_BUTTON = "{n} · ✓"
DISMISSED_BUTTON = "{n} · ✖"

ADDED_TOAST = "Добавлено: «{text}»."
ADDED_AND_RUN_TOAST = "Добавлено, ищу: «{text}». Карточки появятся в /notes."
DISMISSED_TOAST = "Больше не предложу."

SUGGEST_CALLBACK_PREFIX = "is:"
# `is:<a|r|x>:<suggestion id>:<epoch>`, matched whole with fullmatch
# (`$` would allow a trailing newline), ASCII digits only. The longest,
# `is:r:2147483647:abcdef`, is 22 bytes, well inside Telegram's 64.
SUGGEST_CALLBACK_RE = re.compile(r"is:(a|r|x):([0-9]+):([a-z2-7]{6})")
# interest_suggestion.id is an INTEGER; a larger one is simply not one.
_SUGGESTION_ID_MAX = 2**31 - 1


def suggestion_callback_data(action: str, suggestion_id: int, epoch: str) -> str:
    """`is:<a|r|x>:<suggestion id>:<epoch>`."""
    return f"{SUGGEST_CALLBACK_PREFIX}{action}:{suggestion_id}:{epoch}"


def parse_suggestion_callback(data: str | None) -> tuple[str, int, str] | None:
    """(action, suggestion id, epoch), the action one of `suggest.ADD`,
    `suggest.ADD_AND_RUN`, `suggest.DISMISS` -- or None for anything
    else, which the handler answers as stale."""
    match = SUGGEST_CALLBACK_RE.fullmatch(data or "")
    if match is None:
        return None
    suggestion_id = int(match.group(2))
    if not 0 < suggestion_id <= _SUGGESTION_ID_MAX:
        return None
    return match.group(1), suggestion_id, match.group(3)


def _one_line(value: str) -> str:
    return " ".join((value or "").split())


def _cap(value: str, limit: int | None) -> str:
    value = _one_line(value)
    if limit is None or len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)].rstrip() + ELLIPSIS


# Tried in order until the message fits: the `why` cap, None meaning
# whole. With INTEREST_SUGGEST_MAX at most 8, a 100-character topic and a
# 160-character reason, the first step always fits; the rest are there
# so a later, larger cap can never make the send raise.
_WHY_SHORTENING = (None, 80, 40, 0)


def render_suggestions(view: suggest.BatchView, *, limit: int = MESSAGE_LIMIT) -> str:
    """The batch's message text (plan section 2): «Можно поискать:», then
    one numbered line per suggestion in position order -- every one of
    them, decided and expired too, so the numbers the buttons carry hold
    on every re-render. A batch with none is «Новых тем не нашлось.».
    Pure: the same batch renders the same text, so a replayed tap's edit
    is a no-op (app/tg/send.py's `edit_keyboard`)."""
    if not view.suggestions:
        return SUGGESTIONS_NONE
    text = ""
    for why_cap in _WHY_SHORTENING:
        lines = [SUGGESTIONS_HEADER]
        for number, item in enumerate(view.suggestions, start=1):
            if why_cap == 0:
                line = SUGGESTION_HEAD.format(n=number, text=_one_line(item.text), packet=item.packet)
            else:
                line = SUGGESTION_LINE.format(
                    n=number, text=_one_line(item.text), packet=item.packet,
                    why=_cap(item.why, why_cap),
                )
            lines.append(line)
        text = "\n".join(lines)
        if len(text) <= limit:
            return text
    return text[: limit - 1] + ELLIPSIS


def suggestions_keyboard(view: suggest.BatchView) -> InlineKeyboardMarkup | None:
    """One row per suggestion, numbered as in the text: an open one has
    «N · добавить», «N · добавить и искать» and «N · ✖»; an added one is
    «N · ✓» and a dismissed one «N · ✖», one button each, whose tap can
    only be stale; an expired one has no row. None when no row is left,
    which removes the keyboard."""
    rows: list[list[InlineKeyboardButton]] = []
    for number, item in enumerate(view.suggestions, start=1):
        if item.status == suggest.OPEN:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=ADD_BUTTON.format(n=number),
                        callback_data=suggestion_callback_data(suggest.ADD, item.id, view.epoch),
                    ),
                    InlineKeyboardButton(
                        text=ADD_AND_RUN_BUTTON.format(n=number),
                        callback_data=suggestion_callback_data(
                            suggest.ADD_AND_RUN, item.id, view.epoch
                        ),
                    ),
                    InlineKeyboardButton(
                        text=DISMISS_BUTTON.format(n=number),
                        callback_data=suggestion_callback_data(
                            suggest.DISMISS, item.id, view.epoch
                        ),
                    ),
                ]
            )
        elif item.status == suggest.ADDED:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=ADDED_BUTTON.format(n=number),
                        callback_data=suggestion_callback_data(suggest.ADD, item.id, view.epoch),
                    )
                ]
            )
        elif item.status == suggest.DISMISSED:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=DISMISSED_BUTTON.format(n=number),
                        callback_data=suggestion_callback_data(
                            suggest.DISMISS, item.id, view.epoch
                        ),
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# One lock per event loop: the heartbeat loop and the claim loop (a job's
# hook, a command's re-send) run in the same process and could otherwise
# both find the same unsent batch and send it twice. Keyed by loop, since
# an asyncio.Lock is bound to the loop it first waits on.
_send_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _send_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _send_locks.get(loop)
    if lock is None:
        lock = _send_locks[loop] = asyncio.Lock()
    return lock


async def _deliver(
    sessionmaker, bot: Bot, clock: Clock, *, chat_id: int, view: suggest.BatchView
) -> bool:
    """Send the batch's message, then stamp it. A failed send propagates
    and leaves the batch unsent for the next pass."""
    message_id = await send_keyboard(
        bot, chat_id, render_suggestions(view), suggestions_keyboard(view)
    )
    async with sessionmaker() as session:
        await suggest.mark_sent(session, view.id, message_id, clock.now_utc())
        await session.commit()
    logger.info(
        "suggestion batch sent",
        extra={"suggest_batch_id": view.id, "count": len(view.suggestions), "event": view.origin},
    )
    return True


async def send_suggestions(sessionmaker, bot: Bot, settings: Settings, clock: Clock) -> bool:
    """The suggestion batch's one message, if a batch is waiting and
    nothing holds it (anchor-interest-suggestions-plan.md section 5,
    step 4); True iff a message went out.

    First every batch left unsent past `suggest.UNSENT_TTL` has its
    suggestions expired (`expire_unsent`), so its buttons, should a
    later copy ever surface, are stale. Then the newest finished, unsent
    batch (`unsent_batch`) is sent while `RESEARCH_ENABLED` is on and
    `may_report_now` allows it: a pause, /quiet or quiet hours hold it,
    and a hold sends nothing and loses nothing. app/worker.py calls this
    after every INTEREST_SUGGEST job and idle run, and on every
    heartbeat tick -- one cheap query when nothing waits."""
    if not settings.RESEARCH_ENABLED:
        return False
    async with _send_lock():
        now = clock.now_utc()
        async with sessionmaker() as session:
            expired = await suggest.expire_unsent(session, now)
            if expired:
                await session.commit()
                logger.info(
                    "suggestions expired unsent", extra={"count": expired, "event": "unsent"}
                )
            view = await suggest.unsent_batch(session, now)
            if view is None:
                return False
            user_state = await get_state(session)
        if not may_report_now(settings, clock, user_state):
            logger.info(
                "suggestion batch held", extra={"suggest_batch_id": view.id, "event": "quiet"}
            )
            return False
        return await _deliver(sessionmaker, bot, clock, chat_id=user_state.chat_id, view=view)


async def _resend(sessionmaker, bot: Bot, clock: Clock, *, chat_id: int, batch_id: int) -> bool:
    """`run_suggest`'s re-send of a finished batch that never went out.
    No `may_report_now`: the user has just asked for it. False when the
    batch is no longer the one waiting (another pass sent it first)."""
    async with _send_lock():
        async with sessionmaker() as session:
            view = await suggest.unsent_batch(session, clock.now_utc())
        if view is None or view.id != batch_id:
            return False
        return await _deliver(sessionmaker, bot, clock, chat_id=chat_id, view=view)


async def run_suggest(
    sessionmaker,
    bot: Bot,
    settings: Settings,
    clock: Clock,
    *,
    chat_id: int,
    timezone: str,
) -> str | None:
    """`/interests suggest`. Returns the reply text for the caller to
    send, or None when the reply was the batch's own message.

    One call into `app/research/suggest.enqueue_suggest`, which owns the
    check order (disabled, a re-send, nothing to suggest from, no
    packet, busy, the quota, the cap -- its own docstring); this turns
    the answer into Russian. A queued batch is made by the worker and
    sent by `send_suggestions`, so the reply only says it is coming. A
    finished batch that never went out is sent here and now, instead of
    a new one being paid for."""
    async with sessionmaker() as session:
        result = await suggest.enqueue_suggest(session, settings, clock, timezone=timezone)
    if result.refusal is not None:
        return SUGGEST_REFUSALS.get(result.refusal, research_ui.DISABLED)
    if result.resend:
        if await _resend(sessionmaker, bot, clock, chat_id=chat_id, batch_id=result.batch_id):
            return None
        return SUGGEST_ALREADY_SENT
    return SUGGEST_ACCEPTED


async def send_batch_failed(
    sessionmaker, bot: Bot, settings: Settings, clock: Clock, *, batch_id: int
) -> bool:
    """The one line a failed manual batch sends in its place
    (`FAILED_LINES`), under the same `may_report_now` hold as the batch
    message; True iff it went out. A weekly batch fails silently: nobody
    asked for it. app/worker.py calls this once, after the
    INTEREST_SUGGEST job is marked done -- a redelivered job runs this
    only if the first run never reached that point."""
    async with sessionmaker() as session:
        batch = await session.get(InterestSuggestBatch, batch_id)
        if batch is None or batch.status != "failed" or batch.origin != suggest.MANUAL:
            return False
        line = FAILED_LINES.get(batch.error_code or "")
        if line is None:
            return False
        user_state = await get_state(session)
    if not may_report_now(settings, clock, user_state):
        logger.info(
            "suggestion failure not sent",
            extra={"suggest_batch_id": batch_id, "error_code": batch.error_code, "event": "quiet"},
        )
        return False
    await bot.send_message(chat_id=user_state.chat_id, text=line)
    logger.info(
        "suggestion failure sent",
        extra={"suggest_batch_id": batch_id, "error_code": batch.error_code},
    )
    return True


# --- the `is:` callback -----------------------------------------------------


async def _run_added(
    sessionmaker, settings: Settings, clock: Clock, *, topic_id: int, text: str
) -> str:
    """An `r` tap's search, after the add was committed: the same
    `enqueue_interest` `/interests run <номер>` calls, with its manual
    quota and money caps. Returns the toast: the run accepted, or the
    topic added and the reason the run was not."""
    async with sessionmaker() as session:
        timezone = (await get_state(session)).timezone
        result = await research_jobs.enqueue_interest(
            session, settings, clock, timezone=timezone, topic_id=topic_id
        )
    if result.refusal is None:
        return ADDED_AND_RUN_TOAST.format(text=text)
    if result.refusal == research_jobs.EMPTY_PACKET:
        reason = research_ui.EMPTY_PACKET_TEMPLATE.format(packet=result.packet)
    else:
        reason = RUN_REFUSALS.get(result.refusal, research_ui.DISABLED)
    return f"{ADDED_TOAST.format(text=text)} {reason}"


async def _rerender(sessionmaker, bot: Bot, *, chat_id: int, message_id: int, batch_id: int) -> None:
    """Edit the tapped message to the batch as it is now. Edit only,
    never a new message (the rule /notes keeps)."""
    async with sessionmaker() as session:
        view = await suggest.batch_view(session, batch_id)
    if view is None:
        return
    await edit_keyboard(
        bot, chat_id, message_id, render_suggestions(view), suggestions_keyboard(view)
    )


async def _drop_keyboard(bot: Bot, chat_id: int, message_id: int) -> None:
    """Remove a dead message's keyboard and nothing else; an already bare
    one is the no-op it should be (app/tg/garden.py's own)."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _after_stale(
    sessionmaker, bot: Bot, *, chat_id: int, message_id: int, suggestion_id: int, epoch: str
) -> None:
    """A stale tap's message, shown as the truth says: re-rendered when
    the suggestion still exists under this epoch (a replay, a decided or
    expired row), its keyboard removed when it does not (a button from
    before /delete, whose id may now name another batch's row -- that
    batch is never drawn onto this message)."""
    async with sessionmaker() as session:
        row = await session.get(InterestSuggestion, suggestion_id)
        batch = await session.get(InterestSuggestBatch, row.batch_id) if row is not None else None
        batch_id = batch.id if batch is not None and batch.epoch == epoch else None
    if batch_id is None:
        await _drop_keyboard(bot, chat_id, message_id)
        return
    await _rerender(sessionmaker, bot, chat_id=chat_id, message_id=message_id, batch_id=batch_id)


async def handle_suggestion_callback(
    sessionmaker,
    bot: Bot,
    settings: Settings,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """A tap on «N · добавить» (`a`), «N · добавить и искать» (`r`) or
    «N · ✖» (`x`) -- plan section 5, step 3.

    `suggest.decide` is the whole tap, in one transaction: the
    conditional UPDATE joined to the batch's epoch (no row: «Устарело.»
    -- a replay, a decided or expired suggestion, a button from before
    /delete, a malformed press), then for `a` and `r` the topic through
    app/core/interests.py's own checks. Their refusal -- the topic cap,
    the risk screen -- rolls the tap back, so the suggestion stays open,
    and its text (`ADD_REFUSALS`, `CAP_TEXT` and `REFUSAL_TEXT` among
    them) is the toast. For `r`, only once that commit is in, the new
    topic goes to `enqueue_interest` (`_run_added`): a refused run never
    undoes the add, and the toast says why it did not run.

    Then the message is re-rendered by edit only; a refusal changed
    nothing, so it is not edited."""
    parsed = parse_suggestion_callback(data)
    if parsed is None:
        logger.info("suggestion press", extra={"event": "malformed"})
        await answer_callback(bot, callback_id, STALE)
        return
    action, suggestion_id, epoch = parsed
    async with sessionmaker() as session:
        decision = await suggest.decide(
            session, settings, suggestion_id=suggestion_id, epoch=epoch, action=action,
            now=clock.now_utc(),
        )
    if decision.outcome == suggest.STALE:
        logger.info("suggestion press", extra={"event": "stale"})
        await answer_callback(bot, callback_id, STALE)
        await _after_stale(
            sessionmaker, bot, chat_id=chat_id, message_id=message_id,
            suggestion_id=suggestion_id, epoch=epoch,
        )
        return
    if decision.outcome != suggest.OK:
        await answer_callback(
            bot, callback_id, ADD_REFUSALS.get(decision.outcome, interests.REFUSAL_TEXT)
        )
        return
    if action == suggest.DISMISS:
        toast = DISMISSED_TOAST
    elif action == suggest.ADD_AND_RUN and decision.topic_id is not None:
        toast = await _run_added(
            sessionmaker, settings, clock, topic_id=decision.topic_id, text=decision.text or ""
        )
    else:
        toast = ADDED_TOAST.format(text=decision.text or "")
    await answer_callback(bot, callback_id, toast)
    await _rerender(
        sessionmaker, bot, chat_id=chat_id, message_id=message_id, batch_id=decision.batch_id
    )


__all__ = [
    "ADDED_AND_RUN_TOAST",
    "ADDED_BUTTON",
    "ADDED_TOAST",
    "ADD_AND_RUN_BUTTON",
    "ADD_BUTTON",
    "ADD_REFUSALS",
    "ADD_USAGE",
    "DISMISSED_BUTTON",
    "DISMISSED_TOAST",
    "DISMISS_BUTTON",
    "FAILED_LINES",
    "LIST_HINT",
    "RUN_ACCEPTED",
    "RUN_NO_TOPICS",
    "RUN_QUOTA_EXHAUSTED",
    "RUN_REFUSALS",
    "RUN_TOPIC_MISSING",
    "RUN_USAGE",
    "STALE",
    "SUGGESTIONS_HEADER",
    "SUGGESTIONS_NONE",
    "SUGGESTION_HEAD",
    "SUGGESTION_LINE",
    "SUGGEST_ACCEPTED",
    "SUGGEST_ALREADY_SENT",
    "SUGGEST_BUSY",
    "SUGGEST_CALLBACK_PREFIX",
    "SUGGEST_CALLBACK_RE",
    "SUGGEST_FAILED",
    "SUGGEST_HINT",
    "SUGGEST_NOTHING",
    "SUGGEST_NO_PACKET",
    "SUGGEST_QUOTA",
    "SUGGEST_REFUSALS",
    "TOPICS_EMPTY",
    "TOPIC_ADDED",
    "TOPIC_TOO_LONG",
    "UNKNOWN_PACKET_REPLY",
    "handle_remove_callback",
    "handle_suggestion_callback",
    "parse_add_args",
    "parse_run_args",
    "parse_suggestion_callback",
    "render_suggestions",
    "render_topic_line",
    "render_topics_list",
    "run_add",
    "run_list",
    "run_now",
    "run_suggest",
    "send_batch_failed",
    "send_suggestions",
    "suggestion_callback_data",
    "suggestions_keyboard",
    "topics_keyboard",
]
