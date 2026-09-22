"""The welfare reply's buttons (plan section 10).

Two buttons, and the asymmetry between them is the point:

- «Я в порядке, продолжаем» turns the persona back on, and does it by
  calling turn.run_resume -- the same function /in calls. Plan section
  13 says persona_active=true may only happen via /in or this button,
  and routing both through one function is what makes that structural
  rather than a convention. tests/test_turn.py greps the whole of app/
  for a second call site.
- «Остаюсь на паузе» does nothing but remove the buttons. Staying
  paused needs no confirmation and writes no state: the persona is
  already off.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.clock import Clock, SystemClock
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

logger = logging.getLogger(__name__)

RESUME = "Я в порядке, продолжаем"
STAY = "Остаюсь на паузе"

STAY_ACK = "Хорошо. Я рядом."


def welfare_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=RESUME, callback_data="w:resume")],
            [InlineKeyboardButton(text=STAY, callback_data="w:stay")],
        ]
    )


async def send_welfare_reply(bot: Bot, chat_id: int, text: str) -> int:
    """Send the out-of-character reply with its two buttons.

    Never split: this message must arrive whole, with its buttons
    attached, rather than as fragments where only the last one is
    actionable.
    """
    return await send_keyboard(bot, chat_id, text, welfare_keyboard())


async def handle_callback(
    sessionmaker,
    bot: Bot,
    settings,
    clock: Clock | None = None,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    update_id: int,
    data: str,
    message_text: str | None = None,
) -> None:
    """`w:resume` / `w:stay`."""
    from app.core import turn

    clock = clock or SystemClock()
    await answer_callback(bot, callback_id)
    action = data.split(":", 1)[1]
    base = (message_text or "").strip()

    if action == "resume":
        # source="button": the audit log is the only record of whether
        # the persona came back by command or by this button.
        await turn.run_resume(
            sessionmaker,
            bot,
            clock=clock,
            chat_id=chat_id,
            update_id=update_id,
            source="button",
        )
        await edit_keyboard(bot, chat_id, message_id, base or RESUME, None)
        return

    await edit_keyboard(bot, chat_id, message_id, f"{base}\n\n{STAY_ACK}" if base else STAY_ACK, None)
