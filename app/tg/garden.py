"""The lens garden's Telegram message and its `lg:` buttons (L3 spec
section 2, anchor-lens-plan.md section 8).

The idle job (app/core/idle/lens_garden.py) records a run and sends
nothing: idle never reaches Telegram. Delivery rides the vault pass
instead. app/worker.py's `_send_garden_cards`, a sibling of the pass's
own update hook with its own try/except, calls `send_pending` after
every VAULT_SYNC pass, so the run's report note has been written by the
time the message can name it, and an unsent run is retried every
minute. It cannot live in app/tg/vault.py: app/tg/lens.py imports that
module, which would make an import cycle once /lens reads the garden.

**One message per run** (the owner's amendment (b) to the L3 spec,
which had one card per gap, up to ~15 at once): a header -- the week,
the counts of new, reopened and older open gaps, and the report's path
once written -- then the gaps, numbered, each with its kind, titles,
proposed title, detail and «снова» when a later run found a gap marked
done still undone. The keyboard has a row per open gap, «N · закрыл»
(`lg:d:<gap id>:<epoch>`) and «N · не нужно» (`lg:n:<gap id>:<epoch>`).
A tap updates that gap, answers the callback and edits the *same*
message: the item gains «— отмечено: …», its row goes, and the keyboard
goes with the last row. The run keeps the gaps it was sent with, in
order (app/vault/lens.py's `run_message_state`), so the numbering holds
on every re-render, even after a later run has reopened one of them
into its own message.

**Sent only while** `LENS_GARDEN_ENABLED` is on, `may_report_now`
allows it (a pause, /quiet, quiet hours: the garden may finish at
night), and `WELFARE_COOLDOWN_H` has passed since `welfare_at` -- the
same hold app/core/outbound_gate.py gives WEEKLY_REVIEW. It is an
out-of-character report like a /read completion line: no Outbound row,
no counters. A hold sends nothing and loses nothing; the run stays
unsent until a pass finds the way clear. A 429 (or any send failure)
propagates to the hook and the next minute's pass tries again. The run
is marked sent after the send: a crash between the two sends the
message twice, and the first copy's buttons are then stale, because
each gap stores the id of the message that carries it.

Telegram's 4096 characters: details, then titles, are shortened until
the message fits (`render`); the report note has them whole.

The epoch in every button is there for the reason it is in `v:`: /delete
restarts identities, so a button from before it must never decide a
gap of the new garden. A press that does not match
`^lg:(d|n|r|a|x):[0-9]+:[a-z2-7]{6}$` exactly (at most 22 bytes) is
stale.

**L4: lens research** (anchor-lens-plan.md section 9, the L4 spec
section 1 with the owner's amendments). Below a live gap's row, a
`missing_note`, `tension` or `bridge` gap gets its own row «N ·
исследовать» (`lg:r:`) while the gap was never researched (it has no
lens job: `research_requested_at` and the job are committed together,
and study jobs are never deleted short of /delete) and research can
run at all (`research_on`: RESEARCH_ENABLED, LENS_ENABLED,
LENS_GARDEN_ENABLED and IDLE_ENABLED, and a non-empty PACKET_LENS). The
tap is one transaction: `lens.request_research` (open -> researched,
once per gap), then `jobs.enqueue_lens_study` (/study's checks and
quota, no queue row, so /study's completion message can never fire);
a refusal rolls both back and the gap stays open. The item then reads
«— исследую, итог придёт отдельным сообщением».

**The result is its own message, as soon as the job finishes** (the
owner's amendment (b), replacing the spec's delivery in the next garden
message). `send_research_results`, called by the same worker hook as
`send_pending` under the same holds, sends one message per finished
lens job: the gap line, up to `RESULT_MAX_CARDS` «• card text
(domain)» lines, «скрыто: H» for hidden cards, and «в Inbox»
(`lg:a:`) / «не нужно» (`lg:x:`). A tap edits that same message with
its outcome and removes the keyboard: «в Inbox» goes through
app/core/echo_write.py (one knowledge note in vaultd's inbox, the gap
to done), «не нужно» rejects the cards and sends the gap back to open.
A job that failed, found nothing or had every card hidden sends a short
«ничего не нашлось» message instead, and the gap goes back to open --
with `research_requested_at` kept, so it is never researched twice.
Either way the garden message is re-rendered, so the gap's own row
reads the truth. `offered_at` on the job and the result message's id
on the gap track the send; a job whose gap has gone (or was resolved
meanwhile) is marked offered with nothing sent.

Telegram only: the router's is_web_sink guard and app/web/ingress.py's
BLOCKED_CALLBACK_PREFIX both refuse `lg:`; the message is never sent to
the web chat.

Logs carry run, job and gap ids, counts and outcomes, never a title,
detail, card text, quote, URL, domain or path (app/log.py), and never a
message id. A research line (L4) never carries a gap id: with it, the
logs would tell a researched gap from a resolved one, which
`lens.gaps()` shows alike as `closed` (the L4 spec section 6). This module reaches the garden only through
app/vault/lens.py (tests/test_vault_notes_isolation.py).
"""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Callable, Mapping

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import echo_write
from app.core.clock import Clock
from app.core.report import may_report_now
from app.core.state import get_state
from app.research import jobs
from app.tg.research import STUDY_REFUSALS
from app.tg.research import DISABLED as RESEARCH_DISABLED
from app.tg.send import MESSAGE_LIMIT, answer_callback, edit_keyboard, send_keyboard
from app.vault import lens
from app.vault.client import VaultClient

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "lg:"
# The callback grammar: `lg:d:` done, `lg:n:` not needed (L3), `lg:r:`
# research, and on a research's result message `lg:a:` into the inbox
# and `lg:x:` not needed (L4). ASCII digits only (`\d` would take any
# Unicode digit) and matched whole, with fullmatch (`$` would allow a
# trailing newline). The longest, `lg:r:2147483647:abcdef`, is 22 bytes,
# well inside Telegram's 64.
CALLBACK_RE = re.compile(r"lg:(d|n|r|a|x):([0-9]+):([a-z2-7]{6})")
DONE = "done"
DISMISSED = "dismissed"
RESEARCH = "research"
ADOPT = "adopt"
DECLINE = "decline"
_ACTIONS = {"d": DONE, "n": DISMISSED, "r": RESEARCH, "a": ADOPT, "x": DECLINE}
# A gap's id is a Postgres integer; a larger one is simply not a gap.
_MAX_ID = 2**31 - 1

HEADER_LINE = "Сад линзы, неделя {week}."
COUNTS_LINE = "Новых: {new} · снова: {reopened} · открыто с прошлых недель: {older}"
REPORT_LINE = "Заметка: {path}"
SHORTENED_LINE = "Текст сокращён, полностью — в заметке."
SHORTENED_NO_REPORT_LINE = "Текст сокращён."

KIND_LABELS = {
    "link": "Связь",
    "missing_note": "Нет заметки",
    "tension": "Противоречие",
    "bridge": "Мост",
}
AGAIN_MARK = " (снова)"
MENTIONED_BY = " — упоминают {titles}"
MARK_DONE = "— отмечено: закрыл (проверю в следующем саду)"
MARK_DISMISSED = "— отмечено: не нужно"
MARK_CLOSED = "— закрыто"
MARK_MOVED = "— перенесено в новое сообщение сада"
# L4: a gap's research, as its garden item shows it.
MARK_RESEARCHING = "— исследую, итог придёт отдельным сообщением"
MARK_RESEARCHED = "— исследовано, итог — в отдельном сообщении"
MARK_ADOPTED = "— записано в Inbox (проверю в следующем саду)"
MARK_RESEARCH_SPENT = "— исследовано, в Inbox ничего не записано"

DONE_BUTTON = "{n} · закрыл"
DISMISS_BUTTON = "{n} · не нужно"
RESEARCH_BUTTON = "{n} · исследовать"

DONE_ANSWER = "Отмечено: закрыл."
DISMISSED_ANSWER = "Отмечено: не нужно."
RESEARCH_ANSWER = "Исследую."
STALE_ANSWER = "Устарело"

# L4, the owner's amendment (b): a finished research's own message.
RESULT_HEADER = "Сад линзы: исследование."
RESULT_CARD_LINE = "• {text} ({domain})"
RESULT_MORE_LINE = "Ещё карточек: {n} — войдут в заметку."
RESULT_HIDDEN_LINE = "скрыто: {n}"
RESULT_NOTHING_LINE = "Ничего не нашлось. Пункт снова открыт в сообщении сада."
RESULT_MAX_CARDS = 6
ADOPT_BUTTON = "в Inbox"
DECLINE_BUTTON = "не нужно"
# What a tap on the result message appends to it (and the keyboard goes).
RESULT_ADOPTED = "— записано в Inbox. Отменить: /lens undo"
RESULT_DECLINED = "— не нужно"
RESULT_EMPTY = "— карточек не осталось"
_RESULT_OUTCOMES = (RESULT_ADOPTED, RESULT_DECLINED, RESULT_EMPTY)
ADOPTED_ANSWER = "Записано в Inbox."
EMPTY_ANSWER = "Карточек не осталось."
REFUSED_ANSWER = "Не получилось записать в Inbox."
UNAVAILABLE_ANSWER = "Хранилище не ответило. Попробуй ещё раз."

ELLIPSIS = "…"
# Tried in order until the message fits: (detail cap, title cap), None
# meaning whole and 0 leaving the detail out.
_SHORTENING = ((None, None), (200, None), (120, None), (60, 60), (0, 40), (0, 20))
# The result message's steps: (card text cap, title cap).
_RESULT_SHORTENING = ((300, None), (160, 60), (80, 40))

HELD_OFF = "off"
HELD_QUIET = "quiet"
HELD_WELFARE = "welfare"


# --- the message -----------------------------------------------------------------


def _one_line(value: str) -> str:
    """Model and title text on one line: a detail with a line break could
    otherwise pass for another numbered item."""
    return " ".join((value or "").split())


def _cap(value: str, limit: int | None) -> str:
    value = _one_line(value)
    if limit is None or len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)].rstrip() + ELLIPSIS


def _quoted(titles, title_cap: int | None) -> str:
    return ", ".join(f"«{_cap(t, title_cap)}»" for t in titles)


def _gap_line(kind: str, titles, title: str | None, title_cap: int | None) -> str:
    """A gap's kind and notes: «Нет заметки: «X» — упоминают «A»» for a
    missing note, «Мост: «A» — «B»» for the rest. The garden item and
    the result message share it."""
    label = KIND_LABELS.get(kind, kind)
    if kind == "missing_note" and title:
        line = f"{label}: «{_cap(title, title_cap)}»"
        if titles:
            line += MENTIONED_BY.format(titles=_quoted(titles, title_cap))
        return line
    return f"{label}: " + " — ".join(f"«{_cap(t, title_cap)}»" for t in titles)


def research_on(settings: Settings) -> bool:
    """Whether «исследовать» may be offered at all (the L4 spec section 1):
    every switch lens research needs (`jobs.lens_research_enabled`), and
    a packet to search. A dead button is worse than none."""
    return jobs.lens_research_enabled(settings) and bool(settings.PACKET_LENS)


def _live(message: lens.GardenMessage, gap: lens.MessageGap) -> bool:
    """Whether this gap has its row on this message's keyboard: every
    open gap before the message is sent, and afterwards the ones
    app/vault/lens.py still calls actionable (open, and carried by this
    message rather than a later run's)."""
    if message.tg_message_id is None:
        return gap.status == "open"
    return gap.actionable


def _mark(
    message: lens.GardenMessage, gap: lens.MessageGap, research: Mapping[int, str]
) -> str | None:
    outcome = research.get(gap.id)
    if gap.status == "researched":
        return MARK_RESEARCHING if outcome in (None, jobs.RUNNING) else MARK_RESEARCHED
    if gap.status == "done":
        return MARK_ADOPTED if outcome == jobs.ADOPTED else MARK_DONE
    if gap.status == "dismissed":
        return MARK_DISMISSED
    if gap.status == "open":
        if not _live(message, gap):
            return MARK_MOVED
        # Back from a research that found nothing, or whose cards were
        # declined: its row is live again, without «исследовать».
        return MARK_RESEARCH_SPENT if outcome == jobs.SPENT else None
    return MARK_CLOSED


def _item(
    number: int,
    message: lens.GardenMessage,
    gap: lens.MessageGap,
    research: Mapping[int, str],
    detail_cap: int | None,
    title_cap: int | None,
) -> list[str]:
    head = f"{number}. " + _gap_line(gap.kind, gap.titles, gap.title, title_cap)
    if gap.reopened:
        head += AGAIN_MARK
    lines = [head]
    if detail_cap != 0 and _one_line(gap.detail):
        lines.append(_cap(gap.detail, detail_cap))
    mark = _mark(message, gap, research)
    if mark is not None:
        lines.append(mark)
    return lines


def _compose(
    message: lens.GardenMessage,
    research: Mapping[int, str],
    detail_cap: int | None,
    title_cap: int | None,
) -> str:
    lines = [
        HEADER_LINE.format(week=message.iso_week),
        COUNTS_LINE.format(
            new=message.new, reopened=message.reopened, older=message.older_open
        ),
    ]
    if message.report_path:
        lines.append(REPORT_LINE.format(path=message.report_path))
    if detail_cap is not None or title_cap is not None:
        lines.append(SHORTENED_LINE if message.report_path else SHORTENED_NO_REPORT_LINE)
    for number, gap in enumerate(message.gaps, start=1):
        lines.append("")
        lines.extend(_item(number, message, gap, research, detail_cap, title_cap))
    return "\n".join(lines)


def render(
    message: lens.GardenMessage,
    *,
    research: Mapping[int, str] | None = None,
    limit: int = MESSAGE_LIMIT,
) -> str:
    """The message's text, at most `limit` characters.

    `research` is `jobs.lens_job_outcomes()` (gap id -> where its
    research stands), for the L4 marks; a gap absent from it was never
    researched.

    Whole when it fits; otherwise details are shortened, then left out,
    and titles shortened, in the steps `_SHORTENING` lists, with a line
    saying so. Only a run with dozens of long-titled gaps could still
    overflow after the last step; that is cut at the limit rather than
    left unsent. Pure: the same state renders the same text, so a
    replayed tap's edit is a no-op (app/tg/send.py's `edit_keyboard`)."""
    research = research or {}
    text = ""
    for detail_cap, title_cap in _SHORTENING:
        text = _compose(message, research, detail_cap, title_cap)
        if len(text) <= limit:
            return text
    return text[: limit - 1] + ELLIPSIS


def callback_data(action: str, gap_id: int, epoch: str) -> str:
    """`lg:<d|n|r|a|x>:<gap id>:<epoch>`."""
    return f"{CALLBACK_PREFIX}{action}:{gap_id}:{epoch}"


def keyboard(
    message: lens.GardenMessage,
    epoch: str,
    *,
    research_on: bool = False,
    research: Mapping[int, str] | None = None,
) -> InlineKeyboardMarkup | None:
    """One row per gap still live on this message, numbered as in the
    text, and (L4) below it a row «N · исследовать» when `research_on`,
    the gap's kind is researchable and it has never been researched (it
    is not in `research`, `jobs.lens_job_outcomes()`). None when no row
    is left, which removes the keyboard."""
    research = research or {}
    rows: list[list[InlineKeyboardButton]] = []
    for number, gap in enumerate(message.gaps, start=1):
        if not _live(message, gap):
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=DONE_BUTTON.format(n=number),
                    callback_data=callback_data("d", gap.id, epoch),
                ),
                InlineKeyboardButton(
                    text=DISMISS_BUTTON.format(n=number),
                    callback_data=callback_data("n", gap.id, epoch),
                ),
            ]
        )
        if research_on and gap.kind in lens.RESEARCHABLE_KINDS and gap.id not in research:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=RESEARCH_BUTTON.format(n=number),
                        callback_data=callback_data("r", gap.id, epoch),
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# --- L4: a research's result message -------------------------------------------------


def _result_compose(
    gap: lens.ResearchGap, cards, hidden: int, card_cap: int, title_cap: int | None
) -> str:
    lines = [RESULT_HEADER, _gap_line(gap.kind, gap.titles, gap.title, title_cap)]
    shown = list(cards)[:RESULT_MAX_CARDS]
    for card in shown:
        lines.append(RESULT_CARD_LINE.format(text=_cap(card.text, card_cap), domain=card.domain))
    if len(cards) > len(shown):
        lines.append(RESULT_MORE_LINE.format(n=len(cards) - len(shown)))
    if hidden:
        lines.append(RESULT_HIDDEN_LINE.format(n=hidden))
    return "\n".join(lines)


def render_result(
    gap: lens.ResearchGap, cards, hidden: int, *, limit: int = MESSAGE_LIMIT
) -> str:
    """A research's result message (the owner's amendment (b)): the gap
    line, up to `RESULT_MAX_CARDS` «• card text (domain)» lines (the
    rest counted: «в Inbox» writes every visible card), and «скрыто: H»
    for cards hidden as high risk. Card text is web-derived and one
    line; it is shortened first, then the titles. Plain text, like every
    message this bot sends: nothing in it is parsed as markup."""
    text = ""
    for card_cap, title_cap in _RESULT_SHORTENING:
        text = _result_compose(gap, cards, hidden, card_cap, title_cap)
        if len(text) <= limit:
            return text
    return text[: limit - 1] + ELLIPSIS


def render_spent(gap: lens.ResearchGap) -> str:
    """The short message for a research that found nothing: it failed,
    went stale, found no card, or every card was hidden."""
    return "\n".join((RESULT_HEADER, _gap_line(gap.kind, gap.titles, gap.title, 60), RESULT_NOTHING_LINE))


def result_keyboard(gap_id: int, epoch: str) -> InlineKeyboardMarkup:
    """«в Inbox» (`lg:a:`) and «не нужно» (`lg:x:`), one row."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=ADOPT_BUTTON, callback_data=callback_data("a", gap_id, epoch)),
                InlineKeyboardButton(text=DECLINE_BUTTON, callback_data=callback_data("x", gap_id, epoch)),
            ]
        ]
    )


def with_outcome(text: str, outcome: str | None) -> str:
    """The result message after a tap: its text as sent, with `outcome`
    as the last line (replacing an earlier outcome line, so a replayed
    tap renders the same text)."""
    lines = (text or "").splitlines()
    while lines and lines[-1] in _RESULT_OUTCOMES:
        lines.pop()
    body = "\n".join(lines)
    tail = "\n" + outcome if outcome else ""
    if len(body) + len(tail) > MESSAGE_LIMIT:
        body = body[: MESSAGE_LIMIT - 1 - len(tail)] + ELLIPSIS
    return body + tail


# --- sending ---------------------------------------------------------------------


def held(settings: Settings, clock: Clock, user_state) -> str | None:
    """Why the message may not go out right now, or None when it may.

    `off`: LENS_GARDEN_ENABLED. `quiet`: `may_report_now` (a pause,
    /quiet, quiet hours). `welfare`: under WELFARE_COOLDOWN_H since the
    welfare trigger, as the outbound gate holds WEEKLY_REVIEW -- a
    weekly list of things to do about the lens is discretionary in the
    same way the review is. L4's result messages wait for the same."""
    if not settings.LENS_GARDEN_ENABLED:
        return HELD_OFF
    if not may_report_now(settings, clock, user_state):
        return HELD_QUIET
    welfare_at = user_state.welfare_at
    if welfare_at is not None and clock.now_utc() - welfare_at < datetime.timedelta(
        hours=settings.WELFARE_COOLDOWN_H
    ):
        return HELD_WELFARE
    return None


async def send_pending(sessionmaker, bot: Bot, settings: Settings, clock: Clock) -> bool:
    """Send the latest run's message if it is unsent and nothing holds it;
    True iff a message went out.

    A run with no gap to show (every proposal deduped, nothing reopened)
    is marked sent without a message: a header with nothing to tap is
    not worth a notification, and /lens and the report already show the
    run. The send comes first, then the mark and its commit, each gap
    stamped with the message's id -- which is what makes its buttons
    live (app/vault/lens.py's `decide_gap`)."""
    if not settings.LENS_GARDEN_ENABLED:
        return False
    async with sessionmaker() as session:
        message = await lens.unsent_run(session)
        if message is None:
            return False
        if not message.gaps:
            await lens.mark_run_sent(session, message.run_id, None, now=clock.now_utc())
            await session.commit()
            logger.info(
                "lens garden message skipped",
                extra={"garden_run_id": message.run_id, "event": "empty"},
            )
            return False
        user_state = await get_state(session)
        reason = held(settings, clock, user_state)
        chat_id, epoch = user_state.chat_id, user_state.vault_epoch
        research = await jobs.lens_job_outcomes(session)
    if reason is not None:
        # Logged only while a run is waiting, not on every idle pass.
        logger.info(
            "lens garden message held",
            extra={"garden_run_id": message.run_id, "event": reason},
        )
        return False
    message_id = await send_keyboard(
        bot,
        chat_id,
        render(message, research=research),
        keyboard(message, epoch, research_on=research_on(settings), research=research),
    )
    async with sessionmaker() as session:
        await lens.mark_run_sent(
            session,
            message.run_id,
            message_id,
            gap_ids=[gap.id for gap in message.gaps],
            now=clock.now_utc(),
        )
        await session.commit()
    return True


async def send_research_results(
    sessionmaker,
    bot: Bot,
    settings: Settings,
    clock: Clock,
    *,
    client_factory: Callable[[Settings], VaultClient] | None = None,
) -> int:
    """Send every finished lens research's result message that nothing
    holds (the owner's amendment (b)); returns how many went out.

    First an inbox write whose answer was lost and that no tap will
    replay any more (its gap resolved by a recheck, reopened or gone) is
    settled with vaultd (`echo_write.settle_orphans`), a result message
    whose cards all expired untapped gives its gap back
    (`_close_spent_results`, settling such a write first), and a lens job unfinished after
    `jobs.LENS_STALE_DAYS` fails as stale (idle may be off: the gap must
    not wait forever), so it is sent below like any spent job. Then each finished, unsent job
    (`jobs.unsent_lens_results`, oldest first):

    - its gap gone, or no longer `researched` (a recheck resolved it
      meanwhile): marked offered, nothing sent;
    - `READY`: the result message with «в Inbox» / «не нужно»; the gap
      records the message's id, which is what makes those buttons live
      (`lens.research_target`);
    - `SPENT`: «ничего не нашлось», and the gap goes back to open with
      its garden row (`lens.reopen_researched`); the garden message is
      re-rendered to show it.

    The same holds as the garden message (`held`). Out of character like
    it: no Outbound row, no counters. The send comes first, then
    `offered_at` and the gap's update in one commit: a crash between
    them sends the result twice, and the first copy's buttons are then
    stale. A failed send propagates to the worker's hook, and the next
    pass retries the rest."""
    if not settings.LENS_GARDEN_ENABLED:
        return 0
    # Looked up per call, not bound at import (tests swap the factory).
    client_factory = client_factory or VaultClient.from_settings
    await _settle_orphans(sessionmaker, settings, clock, client_factory)
    await _close_spent_results(sessionmaker, bot, settings, clock, client_factory)
    now = clock.now_utc()
    async with sessionmaker() as session:
        if await jobs.fail_stale_lens_jobs(session, now):
            await session.commit()
        results = await jobs.unsent_lens_results(session)
        if not results:
            return 0
        gaps = await lens.research_gaps(
            session, [result.gap_id for result in results if result.gap_id is not None]
        )
        orphans = [
            result.job_id
            for result in results
            if result.gap_id is None
            or result.gap_id not in gaps
            or gaps[result.gap_id].status != "researched"
        ]
        if orphans:
            await jobs.mark_offered(session, orphans, now)
            await session.commit()
            logger.info(
                "lens research result dropped", extra={"count": len(orphans), "event": "gap_gone"}
            )
        pending = [result for result in results if result.job_id not in orphans]
        if not pending:
            return 0
        user_state = await get_state(session)
        reason = held(settings, clock, user_state)
        chat_id, epoch = user_state.chat_id, user_state.vault_epoch
    if reason is not None:
        logger.info(
            "lens research result held", extra={"count": len(pending), "event": reason}
        )
        return 0
    sent = 0
    for result in pending:
        gap = gaps[result.gap_id]
        if result.outcome == jobs.READY:
            text = render_result(gap, result.cards, result.hidden)
            markup = result_keyboard(gap.id, epoch)
        else:
            text, markup = render_spent(gap), None
        message_id = await send_keyboard(bot, chat_id, text, markup)
        async with sessionmaker() as session:
            await jobs.mark_offered(session, [result.job_id], clock.now_utc())
            if result.outcome == jobs.READY:
                await lens.mark_research_sent(session, gap.id, message_id)
            else:
                await lens.reopen_researched(session, gap.id)
            await session.commit()
        sent += 1
        logger.info(
            "lens research result sent",
            extra={
                "job_id": result.job_id,
                "event": result.outcome,
                "cards": len(result.cards),
                "count": result.hidden,
            },
        )
        if result.outcome != jobs.READY:
            await _refresh_garden(sessionmaker, bot, settings, gap.id)
    return sent


async def _settle_orphans(
    sessionmaker, settings: Settings, clock: Clock, client_factory
) -> int:
    """Unconfirmed inbox writes no tap reaches any more (app/core/
    echo_write.py's `settle_open`): each is confirmed if vaultd holds it
    -- so `/lens undo` can take it back -- or deleted if not. A vault
    that does not answer leaves them for the next pass. Returns how many
    were settled."""
    async with sessionmaker() as session:
        rows = await echo_write.orphan_rows(session)
        if not rows:
            return 0
        client = client_factory(settings)
        settled = 0
        for row in rows:
            if await echo_write.settle_open(session, client, clock, row) != echo_write.UNAVAILABLE:
                settled += 1
    return settled


async def _close_spent_results(
    sessionmaker, bot: Bot, settings: Settings, clock: Clock, client_factory
) -> int:
    """Result messages whose cards all expired untapped (sweeps expire a
    lens card `RESEARCH_CARD_TTL_DAYS` after its message went out): the
    gap would otherwise stay `researched` for good, with no row in the
    garden message and buttons that can only answer «Карточек не
    осталось». Each goes back to open, as a spent research does, its
    result message loses its keyboard, and the garden message is
    re-rendered. Only gaps whose result message was sent
    (`research_message_id`): an unsent spent job is `send_research_results`'
    to report. No hold applies: nothing new is sent, and an edit makes no
    sound. A gap with an unconfirmed inbox write (a «в Inbox» whose answer
    was lost, its cards expired since) is settled with vaultd first: if
    the note exists the gap is done, not reopened, and if the vault does
    not answer the gap waits for the next pass. Returns how many."""
    async with sessionmaker() as session:
        outcomes = await jobs.lens_job_outcomes(session)
        spent = [gap_id for gap_id, outcome in outcomes.items() if outcome == jobs.SPENT]
        if not spent:
            return 0
        stuck = [
            gap
            for gap in (await lens.research_gaps(session, spent)).values()
            if gap.status == "researched" and gap.research_message_id is not None
        ]
        closed = []
        for gap in stuck:
            row = await echo_write.open_row(session, gap.id)
            if row is not None:
                settled = await echo_write.settle_open(
                    session, client_factory(settings), clock, row
                )
                if settled == echo_write.UNAVAILABLE:
                    continue
                if settled == echo_write.OK:
                    # The note was written after all: the gap is done.
                    closed.append(gap)
                    continue
            if await lens.reopen_researched(session, gap.id):
                closed.append(gap)
        await session.commit()
        chat_id = (await get_state(session)).chat_id if closed else None
    for gap in closed:
        logger.info("lens research result expired", extra={"event": "expired"})
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=gap.research_message_id, reply_markup=None
            )
        except Exception as exc:  # noqa: BLE001 - the gap is already reopened
            logger.warning(
                "lens research keyboard not removed",
                extra={"event": type(exc).__name__},
            )
        await _refresh_garden(sessionmaker, bot, settings, gap.id)
    return len(closed)


async def _refresh_garden(sessionmaker, bot: Bot, settings: Settings, gap_id: int) -> None:
    """Re-render the garden message that carries this gap, after its
    research changed what the item should say (a result adopted,
    declined or spent). The gap's run is the one whose message carries
    it: a researched gap is never moved to a later run. Best effort: a
    failed edit is logged by type and the next tap on that message
    re-renders it anyway."""
    try:
        async with sessionmaker() as session:
            known = {gap.id: gap for gap in await lens.known_gaps(session)}
            gap = known.get(gap_id)
            if gap is None:
                return
            state = await lens.run_message_state(session, gap.garden_run_id)
            if state is None or state.tg_message_id is None:
                return
            research = await jobs.lens_job_outcomes(session)
            user_state = await get_state(session)
        await edit_keyboard(
            bot,
            user_state.chat_id,
            state.tg_message_id,
            render(state, research=research),
            keyboard(
                state,
                user_state.vault_epoch,
                research_on=research_on(settings),
                research=research,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - the garden message is secondary
        logger.warning(
            "lens garden refresh failed", extra={"event": type(exc).__name__}
        )


# --- the `lg:` callback ----------------------------------------------------------


def parse_callback(data: str | None) -> tuple[str, int, str] | None:
    """(action, gap id, epoch) -- the action one of `done`, `dismissed`,
    `research`, `adopt`, `decline` -- or None for anything else."""
    match = CALLBACK_RE.fullmatch(data or "")
    if match is None:
        return None
    gap_id = int(match.group(2))
    if not 0 < gap_id <= _MAX_ID:
        return None
    return _ACTIONS[match.group(1)], gap_id, match.group(3)


async def _garden_view(session, message_id: int):
    """This message's run as it reads now, the research outcomes, and the
    current epoch; the state is None when the message is not a run's."""
    run_id = await lens.message_run_id(session, message_id)
    state = await lens.run_message_state(session, run_id) if run_id is not None else None
    research = await jobs.lens_job_outcomes(session) if state is not None else {}
    current_epoch = (await get_state(session)).vault_epoch
    return state, research, current_epoch


async def handle_callback(
    sessionmaker,
    bot: Bot,
    clock: Clock,
    *,
    settings: Settings,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
    message_text: str | None = None,
    client_factory: Callable[[Settings], VaultClient] = VaultClient.from_settings,
) -> None:
    """A tap on «N · закрыл», «N · не нужно» or «N · исследовать» on a
    garden message, or on «в Inbox» / «не нужно» on a research's result
    message (L4; `message_text` is that message's text as Telegram hands
    it back, which the edit keeps).

    `lens.decide_gap` moves an open gap that this very message carries
    to done or dismissed; anything else -- a malformed press, a wrong
    epoch, a replay, a gap decided or reopened into a later run's
    message -- is stale and answers «Устарело». Either way, when the
    press came from a run's message, that message is re-rendered from
    the database and edited in place: the tapped item gains its mark and
    loses its row, and a stale press leaves it as the truth says
    (`edit_keyboard` swallows the no-op edit)."""
    parsed = parse_callback(data)
    if parsed is None:
        logger.info("lens garden press", extra={"event": "malformed"})
        await answer_callback(bot, callback_id, STALE_ANSWER)
        return
    action, gap_id, epoch = parsed
    if action == RESEARCH:
        await _handle_research(
            sessionmaker, bot, clock, settings, callback_id=callback_id, chat_id=chat_id,
            message_id=message_id, gap_id=gap_id, epoch=epoch,
        )
        return
    if action in (ADOPT, DECLINE):
        await _handle_result(
            sessionmaker, bot, clock, settings, client_factory, action=action,
            callback_id=callback_id, chat_id=chat_id, message_id=message_id,
            gap_id=gap_id, epoch=epoch, message_text=message_text,
        )
        return
    async with sessionmaker() as session:
        outcome = await lens.decide_gap(
            session, gap_id, epoch, action, clock.now_utc(), message_id=message_id
        )
        await session.commit()
        state, research, current_epoch = await _garden_view(session, message_id)
    if outcome == "ok":
        await answer_callback(
            bot, callback_id, DONE_ANSWER if action == DONE else DISMISSED_ANSWER
        )
    else:
        await answer_callback(bot, callback_id, STALE_ANSWER)
    if state is None:
        return
    await edit_keyboard(
        bot,
        chat_id,
        message_id,
        render(state, research=research),
        keyboard(state, current_epoch, research_on=research_on(settings), research=research),
    )


async def _handle_research(
    sessionmaker,
    bot: Bot,
    clock: Clock,
    settings: Settings,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    gap_id: int,
    epoch: str,
) -> None:
    """«N · исследовать» (the L4 spec section 1): one transaction.
    `lens.request_research` moves the open gap this message carries to
    `researched` (stale otherwise: a wrong epoch, a replay, a `link`
    gap, a gap already researched once), then `jobs.enqueue_lens_study`
    queues the job with /study's checks, in /study's order and with its
    quota, spent here at the tap. A refusal rolls both back, so the gap
    stays open: «Исследования выключены.» (a switch off, or an empty
    PACKET_LENS), or /study's quota and budget texts. Nothing else is
    sent: the result comes as its own message once the job finishes
    (`send_research_results`)."""
    code = None
    async with sessionmaker() as session:
        timezone = (await get_state(session)).timezone
        outcome = await lens.request_research(
            session, gap_id, epoch, clock.now_utc(), message_id=message_id
        )
        if outcome == "ok":
            _job_id, code = await jobs.enqueue_lens_study(
                session, settings, clock, gap_id=gap_id, timezone=timezone
            )
        if outcome == "ok" and code is None:
            await session.commit()
        else:
            await session.rollback()
        state, research, current_epoch = await _garden_view(session, message_id)
    if outcome != "ok":
        answer, event = STALE_ANSWER, "stale"
    elif code is not None:
        answer, event = STUDY_REFUSALS.get(code, RESEARCH_DISABLED), code
    else:
        answer, event = RESEARCH_ANSWER, "researched"
    logger.info("lens garden press", extra={"event": event})
    await answer_callback(bot, callback_id, answer)
    if state is None:
        return
    await edit_keyboard(
        bot,
        chat_id,
        message_id,
        render(state, research=research),
        keyboard(state, current_epoch, research_on=research_on(settings), research=research),
    )


async def _result_base(sessionmaker, gap_id: int, message_text: str | None) -> str:
    """The result message's text to edit: as Telegram handed it back, or,
    when it did not (an inaccessible message), the header and the gap
    line."""
    if message_text:
        return message_text
    async with sessionmaker() as session:
        gap = (await lens.research_gaps(session, [gap_id])).get(gap_id)
    if gap is None:
        return RESULT_HEADER
    return "\n".join((RESULT_HEADER, _gap_line(gap.kind, gap.titles, gap.title, 60)))


async def _handle_result(
    sessionmaker,
    bot: Bot,
    clock: Clock,
    settings: Settings,
    client_factory: Callable[[Settings], VaultClient],
    *,
    action: str,
    callback_id: str,
    chat_id: int,
    message_id: int,
    gap_id: int,
    epoch: str,
    message_text: str | None,
) -> None:
    """«в Inbox» or «не нужно» on a research's result message.

    «в Inbox» is app/core/echo_write.py's `adopt_research`: one knowledge
    note in vaultd's inbox holding every visible card, the cards adopted
    and the gap moved to done. «не нужно» rejects the cards and sends
    the gap back to open (never to be researched again). Either one, or
    finding no card left (expired), edits this same message with the
    outcome and removes its keyboard, and re-renders the garden message.
    A stale tap (a wrong epoch, the gap no longer researched, or not this
    message's) answers «Устарело» and removes the dead keyboard, leaving
    the text as it is (the callback's copy may predate an earlier tap's
    outcome line). A refusal («Не получилось записать в Inbox.») or a
    vault that did not answer keeps the message and its buttons as they
    are: nothing was confirmed, and a second «в Inbox» replays the same
    changeset. «не нужно» after such a lost answer first asks vaultd
    (`echo_write.settle_open`): a note that was written after all makes
    the outcome «записано в Inbox» (so `/lens undo` can take it back),
    and a vault that does not answer keeps the buttons."""
    now = clock.now_utc()
    if action == ADOPT:
        client = client_factory(settings)
        async with sessionmaker() as session:
            outcome = await echo_write.adopt_research(
                session, client, clock, gap_id=gap_id, epoch=epoch, message_id=message_id
            )
            if outcome == echo_write.EMPTY:
                # Every card expired (or was hidden): nothing to write, so
                # the gap is back to open, as for a research that found
                # nothing.
                await lens.reopen_researched(session, gap_id)
                await session.commit()
        answers = {
            echo_write.OK: (ADOPTED_ANSWER, RESULT_ADOPTED),
            echo_write.EMPTY: (EMPTY_ANSWER, RESULT_EMPTY),
            echo_write.REFUSED: (REFUSED_ANSWER, None),
            echo_write.UNAVAILABLE: (UNAVAILABLE_ANSWER, None),
        }
        answer, line = answers.get(outcome, (STALE_ANSWER, None))
        closed = outcome in (echo_write.OK, echo_write.EMPTY)
        stale = outcome not in answers
    else:
        settled = None
        async with sessionmaker() as session:
            target = await lens.research_target(session, gap_id, epoch, message_id=message_id)
            row = await echo_write.open_row(session, gap_id) if target is not None else None
            if row is not None:
                # An earlier «в Inbox» whose answer was lost: vaultd may
                # hold the note, so it is settled before the gap moves.
                settled = await echo_write.settle_open(
                    session, client_factory(settings), clock, row
                )
            if target is not None and settled in (None, echo_write.NOTHING):
                await jobs.reject_lens_cards(session, gap_id, now)
                await lens.reopen_researched(session, gap_id)
                await session.commit()
        if target is None:
            outcome, answer, line = "stale", STALE_ANSWER, None
        elif settled == echo_write.UNAVAILABLE:
            outcome, answer, line = echo_write.UNAVAILABLE, UNAVAILABLE_ANSWER, None
        elif settled == echo_write.OK:
            # The note is in the inbox after all; /lens undo takes it back.
            outcome, answer, line = "adopted_before", ADOPTED_ANSWER, RESULT_ADOPTED
        else:
            outcome, answer, line = "declined", DISMISSED_ANSWER, RESULT_DECLINED
        closed, stale = line is not None, target is None
    logger.info("lens research press", extra={"event": outcome})
    await answer_callback(bot, callback_id, answer)
    if stale:
        # Only the dead keyboard goes: the text is left alone. The
        # callback's copy of it may predate an earlier tap's outcome
        # line (a double tap, a crash replay), and writing it back
        # would erase that line.
        await _drop_keyboard(bot, chat_id, message_id)
        return
    if not closed:
        return
    base = await _result_base(sessionmaker, gap_id, message_text)
    await edit_keyboard(bot, chat_id, message_id, with_outcome(base, line), None)
    await _refresh_garden(sessionmaker, bot, settings, gap_id)


async def _drop_keyboard(bot: Bot, chat_id: int, message_id: int) -> None:
    """Remove a message's keyboard and nothing else; an already bare one
    ("message is not modified") is the no-op it should be, as in
    `edit_keyboard`."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
