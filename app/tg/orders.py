"""Standing orders: the proposal/counter cards, `/order`, `/orders`, and
every `so:*` callback (phase-5 plan sections 3 and 7; milestone 5c).

Everything Telegram-shaped about standing orders lives here; app/core/
orders.py is the domain layer and imports no aiogram types, nothing
under app.tg, and nothing that could write `intensity`, `due_action`,
`focus_on`, `streak` or `persona_active` -- same split as app/tg/
notebook.py and app/core/notebook.py.

**Callbacks**, all three characters inside Telegram's 64-byte limit:
`so:a:<id>` accept, `so:c:<id>` start a counter ("Изменить"), `so:r:<id>`
decline/cancel, `so:x:<id>` retire (`/orders`' own [Снять]). `so:a` and
`so:r` are shared between the original proposal card and the counter
card -- «Принять»/«Принять мой вариант» and «Отклонить»/«Отмена» are the
same two underlying actions (app/core/orders.py's `accept`/`decline`
both work on either a `proposed` or a `countered` row), so one handler
serves both cards. The counter card simply never gets a «c» button --
one round only, and app/core/orders.py's `start_counter` refuses a
second one anyway.

**Idempotency.** `/order` and `/orders` go through router.py's `_once`/
`_reply_once` dance like every other mutating command. The `so:*`
callbacks need no replay gate of their own: `accept`/`decline`/`retire`/
`start_counter` all key off the row's *status*, so a replayed press
lands on a row already decided and gets the same stale answer a
genuinely stale id would.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import orders
from app.core.clock import Clock
from app.db.models import StandingOrder
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

logger = logging.getLogger(__name__)

ACCEPT = "Принять"
ACCEPT_COUNTER = "Принять мой вариант"
CHANGE = "Изменить"
DECLINE = "Отклонить"
CANCEL = "Отмена"
RETIRE = "Снять"

ACCEPTED_TEXT = "✅ Принято"
DECLINED_TEXT = "✖️ Отклонено"
RETIRED_TEXT = "✖️ Снято"
STALE = "Устарело."

ORDERS_LIST_EMPTY = "Договорённостей нет."
ORDER_USAGE = (
    "Как? /order <каденция> <текст>, где каденция — daily, weekdays, "
    "weekly:1-7 (1 — понедельник) или once. Например: "
    "/order daily пить воду по утрам."
)
ORDER_CREATED = "Записал: «{text}» ({cadence})."


def _order_text(order: StandingOrder) -> str:
    return orders.PROPOSAL_TEXT.format(
        text=order.text, cadence=orders.cadence_label(order.cadence, order.weekday)
    )


def proposal_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=ACCEPT, callback_data=f"so:a:{order_id}"),
                InlineKeyboardButton(text=CHANGE, callback_data=f"so:c:{order_id}"),
                InlineKeyboardButton(text=DECLINE, callback_data=f"so:r:{order_id}"),
            ]
        ]
    )


def counter_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=ACCEPT_COUNTER, callback_data=f"so:a:{order_id}"),
                InlineKeyboardButton(text=CANCEL, callback_data=f"so:r:{order_id}"),
            ]
        ]
    )


def _retire_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=RETIRE, callback_data=f"so:x:{order_id}")]]
    )


# --- sending a proposal or a counter card -----------------------------


async def send_order_proposal(sessionmaker, bot: Bot, *, chat_id: int, order_id: int) -> None:
    """The extractor's (and, from 5d, the review's) proposal card.

    A no-op if the order is gone or no longer `proposed` -- the same
    defensive read app/tg/proposals.py's `send_proposal` does, in case
    the send races something that already decided the row (in practice
    nothing does yet, but the shape costs nothing and matches).
    """
    async with sessionmaker() as session:
        order = await session.get(StandingOrder, order_id)
        if order is None or order.status != orders.PROPOSED:
            return
        text = _order_text(order)

    message_id = await send_keyboard(bot, chat_id, text, proposal_keyboard(order_id))
    async with sessionmaker() as session:
        await orders.set_message_id(session, order_id, message_id)


async def send_counter_outcome(
    sessionmaker, bot: Bot, *, chat_id: int, outcome: orders.CounterOutcome
) -> None:
    """What `submit_counter` (app/core/turn.py step 0c) results in.

    `"ok"` shows the counter card; `"refused"` shows the plain refusal
    text with no keyboard; `"stale"` (the original vanished or was
    already decided by the time the counter text arrived -- a race, not
    a normal path) is answered the same way a stale button would be.
    """
    if outcome.status == "ok" and outcome.order is not None:
        text = orders.COUNTER_CARD_TEXT.format(text=outcome.order.text)
        message_id = await send_keyboard(bot, chat_id, text, counter_keyboard(outcome.order.id))
        async with sessionmaker() as session:
            await orders.set_message_id(session, outcome.order.id, message_id)
        return
    if outcome.status == "refused":
        await send_keyboard(bot, chat_id, orders.REFUSAL_TEXT, None)
        return
    await send_keyboard(bot, chat_id, STALE, None)


# --- /order and /orders -------------------------------------------------


async def run_order_command(
    sessionmaker, settings: Settings, clock: Clock, *, text: str
) -> str:
    """`/order <каденция> <текст>`. Returns the reply text."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return ORDER_USAGE
    parsed = orders.parse_cadence(parts[0])
    if parsed is None:
        return ORDER_USAGE
    cadence, weekday = parsed

    async with sessionmaker() as session:
        result = await orders.create_active(
            session, settings, parts[1], cadence, weekday, source="user", clock=clock
        )

    if result == "refused":
        return orders.REFUSAL_TEXT
    if result == "cap":
        return orders.CAP_TEXT
    return ORDER_CREATED.format(
        text=parts[1].strip(), cadence=orders.cadence_label(cadence, weekday)
    )


def render_orders_list(rows: list[StandingOrder]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not rows:
        return ORDERS_LIST_EMPTY, None
    lines = [
        f"«{row.text}» ({orders.cadence_label(row.cadence, row.weekday)})" for row in rows
    ]
    buttons = [
        [InlineKeyboardButton(text=f"{RETIRE} «{row.text[:24]}»", callback_data=f"so:x:{row.id}")]
        for row in rows
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def run_orders_list(sessionmaker, bot: Bot, *, chat_id: int) -> None:
    async with sessionmaker() as session:
        rows = await orders.active_orders(session)
    text, markup = render_orders_list(rows)
    await send_keyboard(bot, chat_id, text, markup)


# --- callbacks -----------------------------------------------------------


async def handle_decision_callback(
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
    """`so:a:<id>` / `so:c:<id>` / `so:r:<id>` -- accept, start a counter,
    or decline/cancel. Shared by the proposal card and the counter card."""
    _, action, raw_id = data.split(":", 2)
    await answer_callback(bot, callback_id)

    try:
        order_id = int(raw_id)
    except ValueError:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        if action == "a":
            result = await orders.accept(session, settings, order_id, clock=clock)
        elif action == "r":
            result = await orders.decline(session, order_id, clock=clock)
        elif action == "c":
            result = await orders.start_counter(session, order_id)
        else:
            await edit_keyboard(bot, chat_id, message_id, STALE, None)
            return
        order = await session.get(StandingOrder, order_id)

    if result == "stale" or order is None:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    if result == "cap":
        # The row keeps its status (plan section 7): the buttons stay
        # live so the user can retry after freeing up capacity.
        base = _order_text(order) if order.counter_of is None else orders.COUNTER_CARD_TEXT.format(
            text=order.text
        )
        keyboard = proposal_keyboard(order_id) if order.counter_of is None else counter_keyboard(order_id)
        await edit_keyboard(bot, chat_id, message_id, f"{base}\n{orders.CAP_TEXT}", keyboard)
        return

    if action == "c":
        await edit_keyboard(bot, chat_id, message_id, orders.COUNTER_PROMPT_TEXT, None)
        return

    outcome_text = ACCEPTED_TEXT if action == "a" else DECLINED_TEXT
    base = _order_text(order) if order.counter_of is None else orders.COUNTER_CARD_TEXT.format(
        text=order.text
    )
    await edit_keyboard(bot, chat_id, message_id, f"{base}\n{outcome_text}", None)


async def handle_retire_callback(
    sessionmaker,
    bot: Bot,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """`so:x:<id>` -- `/orders`' own [Снять], re-rendering the list in place."""
    await answer_callback(bot, callback_id)
    _, _, raw_id = data.split(":", 2)

    try:
        order_id = int(raw_id)
    except ValueError:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        result = await orders.retire(session, order_id, clock=clock)

    if result == "stale":
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        rows = await orders.active_orders(session)
    text, markup = render_orders_list(rows)
    await edit_keyboard(bot, chat_id, message_id, text, markup)
