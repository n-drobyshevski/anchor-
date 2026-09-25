"""`/paid` and the `ob:*` callbacks: the debt queue's Telegram side (phase 5, slice 4).

app/core/obligations.py is the domain layer; everything aiogram-shaped
about debts lives here, the same split as app/tg/orders.py.

- `/paid` lists the open debts, oldest first, each with [Закрыть]
  (`ob:d:<id>`, closes as done) and [Снять] (`ob:x:<id>`, drops it).
- `/paid N` closes the Nth debt of that list as done.

`/done` is not touched: it is the planner's command.

**Idempotency.** `/paid` goes through router.py's `_once`/`_reply_once`
like every other mutating command. The callbacks need no replay gate of
their own: `obligations.close` keys off status='open', so a replayed
press lands on a closed row and gets the stale answer.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core import obligations
from app.core.clock import Clock
from app.db.models import Obligation
from app.tg.send import answer_callback, edit_keyboard, send_keyboard, send_reply

logger = logging.getLogger(__name__)

CLOSE = "Закрыть"
DROP = "Снять"

EMPTY = "Долгов нет."
LIST_HEADER = "Долги (старые сверху):"
CLOSED_TEXT = "✅ Закрыто: «{text}»"
DROPPED_TEXT = "✖️ Снято: «{text}»"
USAGE = "Как? /paid — список долгов, /paid <номер> — закрыть долг с этим номером."
OUT_OF_RANGE = "Долга с номером {n} нет. /paid — список."
STALE = "Устарело."


def _line(index: int, row: Obligation) -> str:
    due = f", до {row.due_local_date:%d.%m}" if row.due_local_date else ""
    return f"{index}. {row.text} (с {row.opened_at:%d.%m}{due})"


def render_list(rows: list[Obligation]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not rows:
        return EMPTY, None
    lines = [LIST_HEADER, *(_line(i, row) for i, row in enumerate(rows, start=1))]
    buttons = [
        [
            InlineKeyboardButton(text=f"{i}. {CLOSE}", callback_data=f"ob:d:{row.id}"),
            InlineKeyboardButton(text=f"{i}. {DROP}", callback_data=f"ob:x:{row.id}"),
        ]
        for i, row in enumerate(rows, start=1)
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def run_paid_list(sessionmaker, bot: Bot, *, chat_id: int) -> None:
    async with sessionmaker() as session:
        rows = await obligations.open_list(session)
    text, markup = render_list(rows)
    if markup is None:
        await send_reply(bot, chat_id, text)
    else:
        await send_keyboard(bot, chat_id, text, markup)


async def run_paid_number(sessionmaker, clock: Clock, *, arg: str) -> str:
    """`/paid N`: close the Nth open debt as done. Returns the reply text."""
    try:
        n = int(arg)
    except ValueError:
        return USAGE
    async with sessionmaker() as session:
        rows = await obligations.open_list(session)
        if not 1 <= n <= len(rows):
            return OUT_OF_RANGE.format(n=n)
        closed = await obligations.close(session, clock, rows[n - 1].id, obligations.DONE)
    if closed is None:
        return STALE
    return CLOSED_TEXT.format(text=closed.text)


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
    """`ob:d:<id>` closes as done, `ob:x:<id>` drops. Re-renders the list."""
    await answer_callback(bot, callback_id)
    try:
        _, action, raw_id = data.split(":", 2)
        obligation_id = int(raw_id)
        status = {"d": obligations.DONE, "x": obligations.DROPPED}[action]
    except (ValueError, KeyError):
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        closed = await obligations.close(session, clock, obligation_id, status)
        rows = await obligations.open_list(session)
    text, markup = render_list(rows)
    if closed is not None:
        template = CLOSED_TEXT if status == obligations.DONE else DROPPED_TEXT
        text = f"{template.format(text=closed.text)}\n\n{text}"
    else:
        text = f"{STALE}\n\n{text}"
    await edit_keyboard(bot, chat_id, message_id, text, markup)
