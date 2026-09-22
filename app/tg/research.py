"""The research commands: /read, /notes, /card, /adopt, /reject (phase-4 plan sections 9, 12).

Everything Telegram-shaped about the research loop lives here, exactly
as app/tg/memory.py owns the memory commands and app/tg/proposals.py
owns the confirm buttons -- those two are this module's templates, and
its shape borrows from both because /notes is the case neither alone
covers: a *paged list* (memory.py) whose *items carry their own decision
buttons* (proposals.py). app/core/cards.py is the domain layer this
module calls into; it imports no aiogram types, and neither this
module's rendering functions do the reverse -- no SQLAlchemy queries
run here, only calls into `app.core.cards` and `app.research.jobs`
(both of which do the actual database and queueing work) and the
Russian text those calls' results turn into.

**Why /notes is one editable message, not one message per card.**
A message-per-card layout would let each card's [Принять]/[Отклонить]
be edited in place independently, which sounds simpler until a replay
enters the picture: `app/db/queue.py`'s stuck-update sweep can redeliver
a callback_query the same way it redelivers a message, and a handler
that *sends new messages* on every delivery -- as paging to a fresh
batch of per-card messages would have to -- duplicates them on a
replay. Every other paged or decided UI in this codebase (`/memories`,
a proposal's buttons) is edit-only for exactly that reason. So instead:
one message holds all `PAGE_SIZE` cards' text and all of their button
rows plus one paging row (`notes_keyboard`), and a decision on any card
in it re-renders that *same* message from page 0 of the current pending
list (`handle_decision_callback`) -- always page 0, because the
callback data (`r:a:<id>` / `r:r:<id>`, deliberately shaped like
proposals.py's `p:a:<id>` / `p:r:<id>`) carries no page number to return
to. A user two pages in who adopts a card lands back on page 1 of
whatever remains; that is a real, deliberate trade-off for an edit-only
implementation, not an oversight -- see the module's tests.

**The adopt/reject reply is a callback toast, not message text.**
Telegram's `answerCallbackQuery` accepts a `text` that pops up as a
brief notification without altering the message, which is exactly
where plan section 9's OOC "Принято в приёмы." belongs: it is
per-presser feedback ("you did that"), not part of the list everyone
who looks at this chat sees. The command path (`/adopt <id>`) sends the
same string as an ordinary reply instead, since there is no toast to
attach it to.
"""

from __future__ import annotations

import urllib.parse

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import cards
from app.core.clock import Clock, SystemClock
from app.db.models import StudyCard
from app.research import errors
from app.research import jobs as research_jobs
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

PAGE_SIZE = cards.PAGE_SIZE

ACCEPT = "Принять"
REJECT_LABEL = "Отклонить"

# Plan section 9's refusal table and the two OOC replies, verbatim.
DISABLED = "Исследования выключены."
QUOTA_EXHAUSTED = "На сегодня лимит поиска исчерпан."
READ_ACCEPTED = "Читаю."
ADOPT_REPLY = "Принято в приёмы."

# Not given by the plan (section 9 states adopt's reply but not
# reject's); chosen to match its register -- short, OOC, the same shape.
REJECT_REPLY = "Отклонено."

STALE = "Устарело."

# /card's row says "for any status except hidden", and section 12 says
# a high-risk card is "never shown" -- so a hidden card and a missing
# one must produce the identical reply, here and from /adopt, /reject
# and their buttons, or the reply itself would leak which ids exist.
CARD_MISSING = "Нет такой карточки."

# Usage prompts. Not in the plan's tables (which cover refusals, not
# missing arguments); shaped like app/tg/memory.py's FORGET_USAGE etc.
READ_USAGE = "Какую страницу прочитать? Напиши так: /read https://example.com/статья."
CARD_USAGE = "Какую карточку показать? Напиши так: /card 12."
ADOPT_USAGE = "Какую карточку принять? Напиши так: /adopt 12."
REJECT_USAGE = "Какую карточку отклонить? Напиши так: /reject 12."

# Chosen; the plan does not specify wording for an empty /notes.
NOTES_EMPTY = "Пока нет карточек."

# app/research/jobs.enqueue_read's refusal codes are a closed set of
# four (DISABLED, QUOTA, CAP, BAD_URL); the plan's refusal table only
# gives text for the first two (shared with /study, section 9), so CAP
# and BAD_URL get replies chosen here, in the same short register.
READ_REFUSALS = {
    research_jobs.DISABLED: DISABLED,
    research_jobs.QUOTA: QUOTA_EXHAUSTED,
    research_jobs.CAP: "На сегодня бюджет на исследования исчерпан.",
    research_jobs.BAD_URL: "Не понимаю эту ссылку.",
}

# --- /study (4c, plan section 9) -------------------------------------------

STUDY_ACCEPTED = "Ищу. Карточки появятся в /notes."

# Verbatim (plan section 9). enqueue_study's UNKNOWN_PACKET is returned
# for anything outside app/research/jobs.PACKETS, so this one string
# covers every bad packet name, not just the ones spelled out here.
UNKNOWN_PACKET_REPLY = "Пакеты: forums, guides, ref."

# Plan section 9 gives this exact line for an unconfigured `guides`
# specifically. `forums` and `ref` ship configured (plan section 3's
# defaults), but nothing in config stops an operator from emptying
# PACKET_FORUMS or PACKET_REF too, and enqueue_study's EMPTY_PACKET
# code does not say which packet it means -- run_study fills that in
# from the packet name the caller already has. One template covers all
# three the same way, and it reads identically to the plan's own
# wording whenever `packet == "guides"`.
EMPTY_PACKET_TEMPLATE = "Пакет {packet} пока не настроен."

# Chosen; the plan states the 200-character cap on study_job.query
# (section 4, ck_study_job_query_length) and requires a refusal rather
# than silent truncation, but gives no wording for it. Plain and
# specific, same register as READ_USAGE. The number is
# research_jobs.QUERY_MAX, not a second constant, so the message can
# never drift from the limit enqueue_study actually enforces.
STUDY_TOPIC_TOO_LONG = (
    f"Слишком длинная тема — уложись в {research_jobs.QUERY_MAX} символов."
)

# Chosen; the plan's table covers refusals, not a bare /study or a
# /study with a packet and no topic. One message covers both cases,
# the same way READ_USAGE does not distinguish "no args" from "args
# but unusable" for /read.
STUDY_USAGE = "Что изучить? Напиши так: /study forums бессонница."

# enqueue_study's refusal codes (app/research/jobs.py). EMPTY_PACKET is
# not here -- it needs the packet name the caller already has, so
# run_study formats EMPTY_PACKET_TEMPLATE itself instead of looking it
# up. EMPTY_TOPIC is here only so this dict is a total map over the
# closed set of codes, matching READ_REFUSALS' shape; parse_study_args
# already refuses a blank topic before enqueue_study ever runs, so the
# router cannot actually produce it.
STUDY_REFUSALS = {
    research_jobs.DISABLED: DISABLED,
    research_jobs.QUOTA: QUOTA_EXHAUSTED,
    research_jobs.CAP: READ_REFUSALS[research_jobs.CAP],
    research_jobs.UNKNOWN_PACKET: UNKNOWN_PACKET_REPLY,
    research_jobs.TOPIC_TOO_LONG: STUDY_TOPIC_TOO_LONG,
    research_jobs.EMPTY_TOPIC: STUDY_USAGE,
}

# --- job completion (worker.py sends these; this module only writes them) ---

DONE_TEXT = "Готово: {n} {noun}. /notes"
# Plan section 9 writes this line as «Готово: N карточек. /notes», with
# N as a placeholder rather than a spec for the three Russian plural
# forms -- and «Готово: 1 карточек» is the kind of sentence a bot that
# is supposed to sound like a person does not write. The wording is
# otherwise exactly the plan's.
CARD_FORMS = ("карточка", "карточки", "карточек")


def card_noun(n: int) -> str:
    """The Russian plural form of «карточка» for `n`.

    The standard rule: 11-14 always take the genitive plural, then the
    last digit decides -- 1 singular, 2-4 genitive singular, else
    genitive plural.
    """
    if n % 100 in range(11, 15):
        return CARD_FORMS[2]
    last = n % 10
    if last == 1:
        return CARD_FORMS[0]
    if last in (2, 3, 4):
        return CARD_FORMS[1]
    return CARD_FORMS[2]


NOTHING_FOUND_TEXT = "Ничего полезного не нашлось."
FAILED_TEXT = "Не получилось: {error}."

# A human-readable (lowercase, no trailing period -- FAILED_TEXT adds
# one) phrase per app/research/errors.py code, plus "cap" for a job the
# spend cap stopped mid-run. Never the raw code and never anything from
# the page itself (plan section 12): these are our own words about our
# own closed set of reasons, not text that touched the web.
ERROR_RU = {
    errors.BLOCKED_SCHEME: "недопустимый протокол ссылки",
    errors.BLOCKED_USERINFO: "ссылка содержит логин и пароль",
    errors.BLOCKED_MALFORMED_URL: "не удалось разобрать ссылку",
    errors.BLOCKED_PRIVATE_IP: "адрес недоступен",
    errors.BLOCKED_DOMAIN: "домен вне разрешённого списка",
    errors.DNS_ERROR: "сайт не найден",
    errors.ROBOTS_DISALLOW: "сайт запрещает чтение этой страницы",
    errors.TOO_MANY_REDIRECTS: "слишком много переадресаций",
    errors.TOO_LARGE: "страница слишком большая",
    errors.BAD_CONTENT_TYPE: "это не текстовая страница",
    errors.TIMEOUT: "сайт не ответил вовремя",
    errors.HTTP_ERROR: "сайт отказал в доступе",
    errors.NETWORK_ERROR: "сетевая ошибка",
    errors.EMPTY_EXTRACTION: "не удалось извлечь текст со страницы",
    research_jobs.CAP: "исчерпан бюджет на это задание",
    # 4d fix: the worker died mid-run. Not a refusal -- nothing
    # refused us -- so the wording says what happened rather than
    # blaming the site.
    research_jobs.INTERRUPTED: "задание прервалось на полпути",
}
# A safe fallback for a code this mapping does not carry. Codes are a
# closed, reviewed set (tests/test_research_isolation.py pins it), so
# this should never actually fire -- it exists so an unmapped code fails
# soft, as a vague-but-honest message, rather than raising or leaking a
# raw internal string to the chat.
ERROR_RU_FALLBACK = "техническая проблема"


def completion_text(*, status: str, error_code: str | None, visible_cards: int) -> str:
    """The one message a finished job earns (plan section 9).

    Takes the outcome's own fields rather than a `ResearchOutcome`
    instance so this module never has to import `app.research.jobs`'
    dataclass -- it already imports the module for the refusal-code
    constants above, but keeping the completion path duck-typed means
    it is exercised by a plain dict in tests without constructing one.
    """
    if status == "done":
        if visible_cards > 0:
            return DONE_TEXT.format(n=visible_cards, noun=card_noun(visible_cards))
        # plan section 7: zero surviving cards is `done`, not `failed`.
        return NOTHING_FOUND_TEXT
    return FAILED_TEXT.format(error=ERROR_RU.get(error_code, ERROR_RU_FALLBACK))


# --- rendering ---


def _domain(url: str) -> str:
    """The host part of a URL, for the compact /notes line.

    Pure string parsing, same as `urllib.parse` anywhere else in the
    stdlib -- not a second fetch, not a lookup, nothing that touches
    the address checks app/research/addresses.py already ran.
    """
    return urllib.parse.urlparse(url).hostname or url


def render_card(card: StudyCard) -> str:
    """One /notes entry (plan section 9): id, kind, text, domain, quote."""
    return (
        f"#{card.id} [{card.kind}]\n"
        f"{card.text}\n"
        f"Источник: {_domain(card.source_url)}\n"
        f"«{card.quote}»"
    )


def render_card_detail(card: StudyCard) -> str:
    """/card <id>'s full view: the whole source URL, not just its domain."""
    lines = [
        f"#{card.id} [{card.kind}]",
        card.text,
        f"Источник: {card.source_url}",
        f"«{card.quote}»",
    ]
    if card.status != "pending":
        lines.append(f"Статус: {card.status}")
    return "\n".join(lines)


def render_notes_page(page_cards: list[StudyCard], total: int, page: int) -> str:
    if total == 0:
        return NOTES_EMPTY
    body = "\n\n".join(render_card(card) for card in page_cards)
    if total > PAGE_SIZE:
        last = min((page + 1) * PAGE_SIZE, total)
        body += f"\n\n({page * PAGE_SIZE + 1}–{last} из {total})"
    return body


def notes_keyboard(
    page_cards: list[StudyCard], page: int, total: int
) -> InlineKeyboardMarkup | None:
    """One [Принять|Отклонить] row per card, plus a paging row (`app/tg/memory.py` style)."""
    rows = [
        [
            InlineKeyboardButton(text=ACCEPT, callback_data=f"r:a:{card.id}"),
            InlineKeyboardButton(text=REJECT_LABEL, callback_data=f"r:r:{card.id}"),
        ]
        for card in page_cards
    ]
    paging = []
    if page > 0:
        paging.append(InlineKeyboardButton(text="‹", callback_data=f"r:p:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        paging.append(InlineKeyboardButton(text="›", callback_data=f"r:p:{page + 1}"))
    if paging:
        rows.append(paging)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def notes_view(
    page_cards: list[StudyCard], total: int, page: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    return render_notes_page(page_cards, total, page), notes_keyboard(page_cards, page, total)


def _decision_reply(action: str, outcome: str) -> str:
    """The text for an adopt/reject outcome, shared by the command and the button.

    `GONE` and `FORBIDDEN` render identically on purpose -- see the
    module docstring on why a hidden card must look like no card at
    all. `ALREADY` renders like a fresh success: idempotent means the
    user sees the same confirmation whether this press is the first
    or the fifth.
    """
    if outcome in (cards.GONE, cards.FORBIDDEN):
        return CARD_MISSING
    return ADOPT_REPLY if action == "a" else REJECT_REPLY


# --- commands ---


async def run_read(
    sessionmaker, settings: Settings, clock: Clock, *, timezone: str, url: str
) -> str:
    """Enqueue a /read job. Returns the reply text; the caller sends it."""
    async with sessionmaker() as session:
        _job_id, refusal = await research_jobs.enqueue_read(
            session, settings, clock, timezone=timezone, url=url
        )
    if refusal is not None:
        return READ_REFUSALS.get(refusal, DISABLED)
    return READ_ACCEPTED


def parse_study_args(raw: str | None) -> tuple[str, str] | None:
    """Split "/study <packet> <тема...>" into `(packet, topic)`.

    None means there is nothing usable to enqueue with: no args at
    all, a packet with nothing after it, or a topic that is only
    whitespace. The router turns None into STUDY_USAGE, the same shape
    as memory.py's parse_id turning an unparseable id into None for a
    usage message.

    The packet name is lowercased here. app/config.py's own packet
    domains are compared case-insensitively (`_parse_packet` lowercases
    them too), and the three names in the plan's command table are
    already lowercase, so this only ever helps a user who capitalizes
    -- it never changes which packet a correctly-typed command reaches.
    The topic is left exactly as typed; only its surrounding whitespace
    is trimmed, since it is Russian free text, not a token.
    """
    if not raw:
        return None
    parts = raw.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    topic = parts[1].strip()
    if not topic:
        return None
    return parts[0].lower(), topic


async def run_study(
    sessionmaker,
    settings: Settings,
    clock: Clock,
    *,
    timezone: str,
    packet: str,
    topic: str,
) -> str:
    """Enqueue a /study job. Returns the reply text; the caller sends it.

    Mirrors run_read: one call into app/research/jobs.py, which owns
    the actual check order (disabled, packet name, packet contents,
    topic length, quota, cap -- its own docstring). This function only
    turns the result into the Russian the plan specifies.
    """
    async with sessionmaker() as session:
        _job_id, refusal = await research_jobs.enqueue_study(
            session, settings, clock, timezone=timezone, packet=packet, topic=topic
        )
    if refusal == research_jobs.EMPTY_PACKET:
        return EMPTY_PACKET_TEMPLATE.format(packet=packet)
    if refusal is not None:
        return STUDY_REFUSALS.get(refusal, DISABLED)
    return STUDY_ACCEPTED


async def run_notes(sessionmaker, bot: Bot, *, chat_id: int) -> None:
    async with sessionmaker() as session:
        page_cards, total = await cards.pending_page(session, page=0)
    text, markup = notes_view(page_cards, total, 0)
    await send_keyboard(bot, chat_id, text, markup)


async def run_card(sessionmaker, *, card_id: int) -> str:
    async with sessionmaker() as session:
        card = await cards.get_card(session, card_id)
    return render_card_detail(card) if card is not None else CARD_MISSING


async def run_adopt(sessionmaker, clock: Clock, *, card_id: int) -> str:
    async with sessionmaker() as session:
        outcome = await cards.adopt(session, card_id, clock=clock)
    return _decision_reply("a", outcome)


async def run_reject(sessionmaker, clock: Clock, *, card_id: int) -> str:
    async with sessionmaker() as session:
        outcome = await cards.reject(session, card_id, clock=clock)
    return _decision_reply("r", outcome)


# --- callbacks ---


async def handle_page_callback(
    sessionmaker, bot: Bot, *, callback_id: str, chat_id: int, message_id: int, data: str
) -> None:
    """`r:p:<page>` -- a /notes paging arrow. Idempotent: an edit to identical
    content is swallowed by `edit_keyboard`, exactly as `app/tg/memory.py`'s does."""
    _, _, raw_page = data.split(":", 2)
    await answer_callback(bot, callback_id)

    try:
        page = max(0, int(raw_page))
    except ValueError:
        return

    async with sessionmaker() as session:
        page_cards, total = await cards.pending_page(session, page=page)
    text, markup = notes_view(page_cards, total, page)
    await edit_keyboard(bot, chat_id, message_id, text, markup)


async def handle_decision_callback(
    sessionmaker,
    bot: Bot,
    clock: Clock | None = None,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """`r:a:<id>` / `r:r:<id>` -- a /notes card's accept/reject buttons.

    Answers with the outcome as a toast (see the module docstring), then
    re-renders the message from page 0 of the current pending list --
    always page 0, and always a full re-render, which is what keeps a
    replayed press harmless: `cards.adopt`/`cards.reject` are themselves
    idempotent, and re-editing to the same content is a no-op.
    """
    _, action, raw_id = data.split(":", 2)
    clock = clock or SystemClock()

    try:
        card_id = int(raw_id)
    except ValueError:
        await answer_callback(bot, callback_id, STALE)
        return

    async with sessionmaker() as session:
        if action == "a":
            outcome = await cards.adopt(session, card_id, clock=clock)
        else:
            outcome = await cards.reject(session, card_id, clock=clock)

    await answer_callback(bot, callback_id, _decision_reply(action, outcome))

    async with sessionmaker() as session:
        page_cards, total = await cards.pending_page(session, page=0)
    text, markup = notes_view(page_cards, total, 0)
    await edit_keyboard(bot, chat_id, message_id, text, markup)
