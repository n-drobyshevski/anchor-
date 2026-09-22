"""Delivering a proactive message, and its one button (phase-3 plan section 7).

Thin on purpose: app/core/outbound_send.py owns the decisions, this
owns the Telegram shape. The split is the same one the rest of the tg/
package keeps -- core never imports aiogram types.

Only the evening nag carries a keyboard. `[Чек-ин]` sends callback
`c:start`, which opens the Phase 2 check-in flow (plan section 10) --
the point of the nag is to make checking in one tap rather than a
remembered command, so a nag without the button would be a worse
version of a message the user did not ask for.

A message with a keyboard is never split (see app/tg/send.py): a
keyboard attaches to exactly one message, and there is no sensible
answer to which chunk gets it. The nag is 1-3 sentences by its own
prompt, so the 4096-char limit is slack, not a target -- but if a
model ever blew past it, send_keyboard raises rather than silently
dropping the button, and the plain path below handles everything else.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.outbound_gate import EVENING_NAG
from app.tg.checkin import START_CALLBACK as CHECKIN_CALLBACK
from app.tg.send import MESSAGE_LIMIT, send_keyboard, send_reply

# CHECKIN_CALLBACK ("c:start", plan section 10) is imported from the
# handler that answers it rather than re-spelled here, so the button
# and its handler cannot drift apart. A typo would otherwise produce a
# button that does nothing, and nothing would fail loudly.
CHECKIN_LABEL = "Чек-ин"


def checkin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=CHECKIN_LABEL, callback_data=CHECKIN_CALLBACK)]
        ]
    )


async def send_outbound_message(bot: Bot, chat_id: int, text: str, *, kind: str) -> None:
    """Send a proactive message, with its kind's buttons if it has any."""
    if kind == EVENING_NAG and len(text) <= MESSAGE_LIMIT:
        await send_keyboard(bot, chat_id, text, checkin_keyboard())
        return
    await send_reply(bot, chat_id, text)
