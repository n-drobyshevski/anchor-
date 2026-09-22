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
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import checkin
from app.core.state import get_state
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

logger = logging.getLogger(__name__)

# Plan section 9, verbatim.
RATING_TEXT = "Как день? (1 — провал, 5 — отлично)"
DUE_TEXT = "Главное действие «{action}» — сделано?"
NOTE_TEXT = "Одной строкой — что важного? Или пропусти."

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


async def start(sessionmaker, bot: Bot, *, chat_id: int, timezone: str) -> None:
    """/checkin: reset today's row and ask the first question."""
    async with sessionmaker() as session:
        row = await checkin.start(session, timezone)
        checkin_id = row.id

    message_id = await send_keyboard(bot, chat_id, RATING_TEXT, rating_keyboard())
    async with sessionmaker() as session:
        await checkin.set_message_id(session, checkin_id, message_id)


async def _current(sessionmaker, timezone: str, message_id: int):
    """Today's check-in, or None if this button belongs to another one."""
    async with sessionmaker() as session:
        row = await checkin.today(session, timezone)
    if row is None or row.tg_message_id != message_id:
        return None
    return row


async def _advance_to_note(sessionmaker, bot: Bot, *, chat_id: int, message_id: int, checkin_id: int):
    async with sessionmaker() as session:
        await checkin.set_awaiting_note(session, checkin_id)
    await edit_keyboard(bot, chat_id, message_id, NOTE_TEXT, note_keyboard())


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
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    update_id: int,
    data: str,
) -> None:
    """`c:r:<n>` / `c:d:<result>` / `c:n:skip`."""
    _, step, value = data.split(":", 2)

    async with sessionmaker() as session:
        user_state = await get_state(session)
    timezone = user_state.timezone

    row = await _current(sessionmaker, timezone, message_id)
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
        await _advance_to_note(
            sessionmaker, bot, chat_id=chat_id, message_id=message_id, checkin_id=row.id
        )
        return

    if step == "d":
        async with sessionmaker() as session:
            if await checkin.set_due_result(session, row.id, value) is None:
                return
        await _advance_to_note(
            sessionmaker, bot, chat_id=chat_id, message_id=message_id, checkin_id=row.id
        )
        return

    if step == "n":
        await finish_and_react(
            sessionmaker,
            bot,
            settings,
            provider,
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

    async with sessionmaker() as session:
        user_state = await get_state(session)
        if user_state.awaiting != checkin.AWAITING_NOTE:
            return
        row, streak = await checkin.finish(session, timezone)
        if row is None:
            return
        line = checkin.synthetic_line(row)

    if message_id is not None:
        await retire(bot, chat_id, message_id, streak)

    await turn.run(
        sessionmaker,
        bot,
        settings,
        provider,
        chat_id=chat_id,
        update_id=update_id,
        user_text=line,
        kind=turn.CHECKIN_KIND,
        extra_flags=[turn.CHECKIN_FLAG],
    )
