"""Sending replies and the typing indicator (plan section 8 steps 4 and 7).

send_reply splits long text (core/split.py) and sends each chunk with
no `parse_mode` -- plain text avoids Telegram entity-parse failures on
whatever the model happens to generate (plan section 11).

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

from aiogram import Bot

from app.core.split import split

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
