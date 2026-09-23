"""The /checkin flow: three steps, one message, edited in place (plan section 9).

Mirrors app/tg/memory.py's paging: a single message whose text and
keyboard are replaced at each step, so the chat is not littered with
four messages per check-in.

**Staleness.** Section 9: "callbacks for a check-in that isn't the
current one just get answer_callback_query('Устарело')". The callback
data it specifies (`c:r:<n>`) carries no check-in id, so the only thing
that can answer "is this the current check-in?" is the message the
button is attached to -- hence `checkin.tg_message_id`. Scrolling up to
last week's check-in and pressing 4 must not rewrite last week.

**Completion.** Steps 1-2 only fill fields. The check-in *finishes*
either when the note arrives as plain text (handled in app/core/turn.py,
which owns that path because a pause word has to be checked first) or
when Пропустить is pressed, which is the only completion this module
performs itself.

5c (phase-5 plan section 7) inserts one step per active standing order
due today, between the due-action step and the note step: `c:o:<id>:
<d|n>`. `_ask_order_or_note` is the fork every completed step (the
rating step's auto-none branch, the due step, and the order step
itself) funnels through -- it asks the next due, unanswered order if
there is one, or falls through to the note step exactly as before.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import checkin, orders
from app.core import clock as clock_module
from app.core.clock import Clock, SystemClock
from app.core.state import get_state
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

logger = logging.getLogger(__name__)

# Plan section 9, verbatim.
RATING_TEXT = "Как день? (1 — провал, 5 — отлично)"
DUE_TEXT = "Главное действие «{action}» — сделано?"
NOTE_TEXT = "Одной строкой — что важного? Или пропусти."

# The evening nag's button (app/tg/outbound.py) fires this.
START_CALLBACK = "c:start"

DUE_LABELS = ((checkin.DONE, "Да"), (checkin.PARTIAL, "Частично"), (checkin.NO, "Нет"))
SKIP = "Пропустить"
STALE = "Устарело."

DONE_TEXT = "Записал. Серия: {streak} дн."


def rating_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=str(n), callback_data=f"c:r:{n}") for n in range(1, 6)]
        ]
    )


def due_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=f"c:d:{value}")
                for value, label in DUE_LABELS
            ]
        ]
    )


def note_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=SKIP, callback_data="c:n:skip")]]
    )


ORDER_DONE = "Да"
ORDER_NO = "Нет"


def order_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=ORDER_DONE, callback_data=f"c:o:{order_id}:d"),
                InlineKeyboardButton(text=ORDER_NO, callback_data=f"c:o:{order_id}:n"),
            ]
        ]
    )


async def start(
    sessionmaker, bot: Bot, clock: Clock, *, chat_id: int, timezone: str
) -> None:
    """/checkin: reset today's row and ask the first question."""
    async with sessionmaker() as session:
        row = await checkin.start(session, clock, timezone)
        checkin_id = row.id

    message_id = await send_keyboard(bot, chat_id, RATING_TEXT, rating_keyboard())
    async with sessionmaker() as session:
        await checkin.set_message_id(session, checkin_id, message_id)


async def _current(sessionmaker, clock: Clock, timezone: str, message_id: int):
    """Today's check-in, or None if this button belongs to another one."""
    async with sessionmaker() as session:
        row = await checkin.today(session, clock, timezone)
    if row is None or row.tg_message_id != message_id:
        return None
    return row


async def _advance_to_note(sessionmaker, bot: Bot, *, chat_id: int, message_id: int, checkin_id: int):
    async with sessionmaker() as session:
        await checkin.set_awaiting_note(session, checkin_id)
    await edit_keyboard(bot, chat_id, message_id, NOTE_TEXT, note_keyboard())


async def _ask_order_or_note(
    sessionmaker,
    settings: Settings,
    clock: Clock,
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    checkin_id: int,
    timezone: str,
) -> None:
    """The fork every completed step before the note funnels through
    (5c, plan section 7): ask the next due, unanswered order, or fall
    through to the note step. Stateless, like app/core/orders.py's
    `next_due_order` itself -- nothing here remembers "which step" a
    check-in is on beyond what that query already answers.
    """
    local_date = clock_module.local_date(clock, timezone)
    async with sessionmaker() as session:
        order = await orders.next_due_order(
            session, checkin_id, local_date, settings.ORDERS_IN_CHECKIN_MAX
        )
    if order is not None:
        await edit_keyboard(
            bot,
            chat_id,
            message_id,
            orders.CHECKIN_STEP_TEXT.format(text=order.text),
            order_keyboard(order.id),
        )
        return
    await _advance_to_note(sessionmaker, bot, chat_id=chat_id, message_id=message_id, checkin_id=checkin_id)


async def retire(bot: Bot, chat_id: int, message_id: int, streak: int) -> None:
    """Replace the check-in message with its result and drop the buttons.

    Called from both completion paths -- here for Пропустить, and from
    app/core/turn.py for a typed note -- so a finished check-in never
    leaves a live button that would finish it again.
    """
    await edit_keyboard(bot, chat_id, message_id, DONE_TEXT.format(streak=streak), None)


async def handle_callback(
    sessionmaker,
    bot: Bot,
    settings: Settings,
    provider,
    safety_provider=None,
    clock: Clock | None = None,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    update_id: int,
    data: str,
) -> None:
    """`c:start` (3b) / `c:r:<n>` / `c:d:<result>` / `c:o:<id>:<d|n>` (5c) / `c:n:skip`."""
    clock = clock or SystemClock()

    async with sessionmaker() as session:
        user_state = await get_state(session)
    timezone = user_state.timezone

    # 3b: the evening nag's [Чек-ин] button (phase-3 plan section 10).
    # Handled before the split below, which expects three parts --
    # `c:start` has two. It opens the flow exactly as typing /checkin
    # does, so a second press just restarts today's check-in, which is
    # already what plan section 9 says a second check-in should do.
    if data == START_CALLBACK:
        await answer_callback(bot, callback_id)
        await start(sessionmaker, bot, clock, chat_id=chat_id, timezone=timezone)
        return

    _, step, value = data.split(":", 2)

    row = await _current(sessionmaker, clock, timezone, message_id)
    if row is None:
        await answer_callback(bot, callback_id, STALE)
        return

    await answer_callback(bot, callback_id)

    if step == "r":
        try:
            rating = int(value)
        except ValueError:
            return
        async with sessionmaker() as session:
            if await checkin.set_rating(session, row.id, rating) is None:
                return

        # Step 2 only exists when there is a main action to report on
        # (plan section 9); otherwise record 'none' and skip straight
        # to the note.
        if user_state.due_action:
            await edit_keyboard(
                bot,
                chat_id,
                message_id,
                DUE_TEXT.format(action=user_state.due_action),
                due_keyboard(),
            )
            return
        async with sessionmaker() as session:
            await checkin.set_due_result(session, row.id, checkin.NONE)
        await _ask_order_or_note(
            sessionmaker, settings, clock, bot,
            chat_id=chat_id, message_id=message_id, checkin_id=row.id, timezone=timezone,
        )
        return

    if step == "d":
        async with sessionmaker() as session:
            if await checkin.set_due_result(session, row.id, value) is None:
                return
        await _ask_order_or_note(
            sessionmaker, settings, clock, bot,
            chat_id=chat_id, message_id=message_id, checkin_id=row.id, timezone=timezone,
        )
        return

    if step == "o":
        # 5c: `value` is `<order_id>:<d|n>` -- the generic `split(":", 2)`
        # above only peeled off the leading `c:o:`, so the order id and
        # the answer letter are still joined here.
        order_id_str, _, letter = value.partition(":")
        try:
            order_id = int(order_id_str)
        except ValueError:
            return
        result = checkin.DONE if letter == "d" else checkin.NO
        async with sessionmaker() as session:
            await orders.record_result(session, row.id, order_id, result, clock=clock)
        await _ask_order_or_note(
            sessionmaker, settings, clock, bot,
            chat_id=chat_id, message_id=message_id, checkin_id=row.id, timezone=timezone,
        )
        return

    if step == "n":
        await finish_and_react(
            sessionmaker,
            bot,
            settings,
            provider,
            safety_provider,
            chat_id=chat_id,
            update_id=update_id,
            message_id=message_id,
            timezone=timezone,
        )


async def finish_and_react(
    sessionmaker,
    bot: Bot,
    settings: Settings,
    provider,
    safety_provider=None,
    clock: Clock | None = None,
    *,
    chat_id: int,
    update_id: int,
    message_id: int | None,
    timezone: str,
) -> None:
    """Complete the check-in with no note, then run the in-character turn.

    The note-supplied path does not come through here: app/core/turn.py
    handles it inline, because a pause word must be matched before the
    text can be treated as a note at all.

    Guarded on the note step still being open. Without it, a second
    Пропустить press -- a replayed callback, or a button left visible by
    a failed edit -- would run (and pay for) a second model call on a
    check-in that is already finished.
    """
    from app.core import turn

    clock = clock or SystemClock()
    async with sessionmaker() as session:
        user_state = await get_state(session)
        if user_state.awaiting != checkin.AWAITING_NOTE:
            return
        row, streak = await checkin.finish(session, clock, timezone)
        if row is None:
            return
        order_results = await orders.results_for_checkin(session, row.id)
        line = checkin.synthetic_line(row, order_results)

    if message_id is not None:
        await retire(bot, chat_id, message_id, streak)

    await turn.run(
        sessionmaker,
        bot,
        settings,
        provider,
        clock=clock,
        chat_id=chat_id,
        update_id=update_id,
        user_text=line,
        kind=turn.CHECKIN_KIND,
        extra_flags=[turn.CHECKIN_FLAG],
        safety_provider=safety_provider,
    )
