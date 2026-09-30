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
done still undone. The keyboard has a row per open gap, «N · сделал»
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
gap of the new garden. «Исследовать» waits for L4, and `lg:r:` is
reserved: until then it is stale, like any press that does not match
`^lg:(d|n):[0-9]+:[a-z2-7]{6}$` exactly.

Telegram only: the router's is_web_sink guard and app/web/ingress.py's
BLOCKED_CALLBACK_PREFIX both refuse `lg:`; the message is never sent to
the web chat.

Logs carry run and gap ids, counts and outcomes, never a title, detail
or path (app/log.py). This module reaches the garden only through
app/vault/lens.py (tests/test_vault_notes_isolation.py).
"""

from __future__ import annotations

import datetime
import logging
import re

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core.clock import Clock
from app.core.report import may_report_now
from app.core.state import get_state
from app.tg.send import MESSAGE_LIMIT, answer_callback, edit_keyboard, send_keyboard
from app.vault import lens

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "lg:"
# The callback grammar the L3 spec gives this module: `lg:d:` done,
# `lg:n:` not needed. ASCII digits only (`\d` would take any Unicode
# digit) and matched whole, with fullmatch (`$` would allow a trailing
# newline).
CALLBACK_RE = re.compile(r"lg:(d|n):([0-9]+):([a-z2-7]{6})")
_ACTIONS = {"d": "done", "n": "dismissed"}
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
MARK_DONE = "— отмечено: сделал (проверю в следующем саду)"
MARK_DISMISSED = "— отмечено: не нужно"
MARK_CLOSED = "— закрыто"
MARK_MOVED = "— перенесено в новое сообщение сада"

DONE_BUTTON = "{n} · сделал"
DISMISS_BUTTON = "{n} · не нужно"

DONE_ANSWER = "Отмечено: сделал."
DISMISSED_ANSWER = "Отмечено: не нужно."
STALE_ANSWER = "Устарело"

ELLIPSIS = "…"
# Tried in order until the message fits: (detail cap, title cap), None
# meaning whole and 0 leaving the detail out.
_SHORTENING = ((None, None), (200, None), (120, None), (60, 60), (0, 40), (0, 20))

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


def _live(message: lens.GardenMessage, gap: lens.MessageGap) -> bool:
    """Whether this gap has its row on this message's keyboard: every
    open gap before the message is sent, and afterwards the ones
    app/vault/lens.py still calls actionable (open, and carried by this
    message rather than a later run's)."""
    if message.tg_message_id is None:
        return gap.status == "open"
    return gap.actionable


def _mark(message: lens.GardenMessage, gap: lens.MessageGap) -> str | None:
    if gap.status == "done":
        return MARK_DONE
    if gap.status == "dismissed":
        return MARK_DISMISSED
    if gap.status == "open":
        return None if _live(message, gap) else MARK_MOVED
    return MARK_CLOSED


def _item(
    number: int,
    message: lens.GardenMessage,
    gap: lens.MessageGap,
    detail_cap: int | None,
    title_cap: int | None,
) -> list[str]:
    label = KIND_LABELS.get(gap.kind, gap.kind)
    if gap.kind == "missing_note" and gap.title:
        head = f"{number}. {label}: «{_cap(gap.title, title_cap)}»"
        if gap.titles:
            head += MENTIONED_BY.format(titles=_quoted(gap.titles, title_cap))
    else:
        head = f"{number}. {label}: " + " — ".join(
            f"«{_cap(t, title_cap)}»" for t in gap.titles
        )
    if gap.reopened:
        head += AGAIN_MARK
    lines = [head]
    if detail_cap != 0 and _one_line(gap.detail):
        lines.append(_cap(gap.detail, detail_cap))
    mark = _mark(message, gap)
    if mark is not None:
        lines.append(mark)
    return lines


def _compose(
    message: lens.GardenMessage, detail_cap: int | None, title_cap: int | None
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
        lines.extend(_item(number, message, gap, detail_cap, title_cap))
    return "\n".join(lines)


def render(message: lens.GardenMessage, *, limit: int = MESSAGE_LIMIT) -> str:
    """The message's text, at most `limit` characters.

    Whole when it fits; otherwise details are shortened, then left out,
    and titles shortened, in the steps `_SHORTENING` lists, with a line
    saying so. Only a run with dozens of long-titled gaps could still
    overflow after the last step; that is cut at the limit rather than
    left unsent. Pure: the same state renders the same text, so a
    replayed tap's edit is a no-op (app/tg/send.py's `edit_keyboard`)."""
    text = ""
    for detail_cap, title_cap in _SHORTENING:
        text = _compose(message, detail_cap, title_cap)
        if len(text) <= limit:
            return text
    return text[: limit - 1] + ELLIPSIS


def callback_data(action: str, gap_id: int, epoch: str) -> str:
    """`lg:d:<gap id>:<epoch>` or `lg:n:<gap id>:<epoch>`."""
    return f"{CALLBACK_PREFIX}{action}:{gap_id}:{epoch}"


def keyboard(message: lens.GardenMessage, epoch: str) -> InlineKeyboardMarkup | None:
    """One row per gap still live on this message, numbered as in the
    text; None when no row is left, which removes the keyboard."""
    rows = [
        [
            InlineKeyboardButton(
                text=DONE_BUTTON.format(n=number), callback_data=callback_data("d", gap.id, epoch)
            ),
            InlineKeyboardButton(
                text=DISMISS_BUTTON.format(n=number),
                callback_data=callback_data("n", gap.id, epoch),
            ),
        ]
        for number, gap in enumerate(message.gaps, start=1)
        if _live(message, gap)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# --- sending ---------------------------------------------------------------------


def held(settings: Settings, clock: Clock, user_state) -> str | None:
    """Why the message may not go out right now, or None when it may.

    `off`: LENS_GARDEN_ENABLED. `quiet`: `may_report_now` (a pause,
    /quiet, quiet hours). `welfare`: under WELFARE_COOLDOWN_H since the
    welfare trigger, as the outbound gate holds WEEKLY_REVIEW -- a
    weekly list of things to do about the lens is discretionary in the
    same way the review is."""
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
    if reason is not None:
        # Logged only while a run is waiting, not on every idle pass.
        logger.info(
            "lens garden message held",
            extra={"garden_run_id": message.run_id, "event": reason},
        )
        return False
    message_id = await send_keyboard(bot, chat_id, render(message), keyboard(message, epoch))
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


# --- the `lg:` callback ----------------------------------------------------------


def parse_callback(data: str | None) -> tuple[str, int, str] | None:
    """(`done` or `dismissed`, gap id, epoch), or None for anything else --
    `lg:r:` (reserved for L4) included."""
    match = CALLBACK_RE.fullmatch(data or "")
    if match is None:
        return None
    gap_id = int(match.group(2))
    if not 0 < gap_id <= _MAX_ID:
        return None
    return _ACTIONS[match.group(1)], gap_id, match.group(3)


async def handle_callback(
    sessionmaker,
    bot: Bot,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """A tap on «N · сделал» or «N · не нужно».

    `lens.decide_gap` moves an open gap that this very message carries
    to done or dismissed; anything else -- a malformed press, `lg:r:`, a
    wrong epoch, a replay, a gap decided or reopened into a later run's
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
    async with sessionmaker() as session:
        outcome = await lens.decide_gap(
            session, gap_id, epoch, action, clock.now_utc(), message_id=message_id
        )
        await session.commit()
        run_id = await lens.message_run_id(session, message_id)
        state = await lens.run_message_state(session, run_id) if run_id is not None else None
        current_epoch = (await get_state(session)).vault_epoch
    if outcome == "ok":
        await answer_callback(
            bot, callback_id, DONE_ANSWER if action == "done" else DISMISSED_ANSWER
        )
    else:
        await answer_callback(bot, callback_id, STALE_ANSWER)
    if state is None:
        return
    await edit_keyboard(bot, chat_id, message_id, render(state), keyboard(state, current_epoch))
