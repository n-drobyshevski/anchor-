"""Sending replies and the typing indicator (plan section 8 steps 4 and 7).

send_reply splits long text (core/split.py) and sends each chunk with
no `parse_mode` -- plain text avoids Telegram entity-parse failures on
whatever the model happens to generate (plan section 11).

2b adds the inline-keyboard primitives (plan section 11). They are
separate functions rather than parameters on send_reply because a
keyboard attaches to exactly one message and send_reply may split its
text across several -- there is no coherent answer to which chunk gets
the buttons. Keyboard payloads here are short by construction (a
memories page is 20 lines of at most ~100 chars), well inside
Telegram's 4096 limit; send_keyboard asserts rather than splitting.

typing_task starts a cancellable background task that refreshes the
"typing" chat action every 4 seconds. Telegram documents a
sendChatAction status as lasting "5 seconds or less", so 4s keeps it
continuously visible without a gap. The caller is responsible for
cancelling the task in a `finally` block once the reply is ready --
this module never cancels itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from app.core.split import split

logger = logging.getLogger(__name__)

# Telegram's hard limit for a single message.
MESSAGE_LIMIT = 4096

# editMessageText rejects an edit that would not change anything. That
# is not an error for us: it happens the first time "‹" is pressed on
# page 1, and on any replayed callback. Matched on the documented
# substring because aiogram surfaces it as a generic TelegramBadRequest.
_NOT_MODIFIED = "message is not modified"

TYPING_REFRESH_SECONDS = 4.0


async def send_reply(bot: Bot, chat_id: int, text: str) -> None:
    """Split `text` and send each chunk as a plain-text message, in order."""
    for chunk in split(text):
        await bot.send_message(chat_id, chunk)


async def _typing_loop(bot: Bot, chat_id: int) -> None:
    while True:
        await bot.send_chat_action(chat_id, "typing")
        await asyncio.sleep(TYPING_REFRESH_SECONDS)


def start_typing(bot: Bot, chat_id: int) -> asyncio.Task:
    """Start the typing-refresh loop as a cancellable task.

    Callers must always cancel this task in a `finally` block:

        task = start_typing(bot, chat_id)
        try:
            ...
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    """
    return asyncio.create_task(_typing_loop(bot, chat_id))


async def stop_typing(task: asyncio.Task) -> None:
    """Cancel a typing task and await its cancellation, swallowing the result."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def send_keyboard(
    bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None
) -> int:
    """Send one message with an inline keyboard; returns its message_id.

    One message, never split: see the module docstring. The message_id
    is returned because callers edit this message in place afterwards
    (paging, showing a result) rather than sending a follow-up.
    """
    if len(text) > MESSAGE_LIMIT:
        raise ValueError(f"keyboard message is {len(text)} chars, limit is {MESSAGE_LIMIT}")
    message = await bot.send_message(chat_id, text, reply_markup=markup)
    return message.message_id


async def edit_keyboard(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit a message in place. Returns False if it was already identical.

    Swallowing "message is not modified" is what makes paging idempotent:
    a replayed callback, or "‹" on the first page, re-renders the same
    content and must be a no-op rather than an error the worker retries.
    Only that one 400 is swallowed; every other TelegramBadRequest
    propagates.
    """
    if len(text) > MESSAGE_LIMIT:
        raise ValueError(f"keyboard message is {len(text)} chars, limit is {MESSAGE_LIMIT}")
    try:
        await bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup
        )
    except TelegramBadRequest as exc:
        if _NOT_MODIFIED in str(exc).lower():
            return False
        raise
    return True


async def answer_callback(bot: Bot, callback_id: str, text: str | None = None) -> None:
    """Acknowledge a button press.

    Always call this, for every callback, including ones that turn out
    to be stale: Telegram spins the button in the client until it is
    answered or times out.
    """
    await bot.answer_callback_query(callback_id, text=text)
