"""`/amendments` and its `am:x:<id>` [Отозвать] button (phase-5 plan
sections 3 and 9; milestone 5d).

Everything Telegram-shaped about persona amendments lives here;
app/core/amendments.py is the domain layer and imports no aiogram
types. `am:a:<id>`/`am:r:<id>` (adopting or declining a `persona_note`
review proposal) live in app/tg/review.py instead -- those two ids are
`review_proposal` ids, not `persona_amendment` ids, and the card that
carries them is one of the review's own proposal cards, not this
module's list.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core import amendments as amendments_module
from app.core.amendments import DisplayRow
from app.core.clock import Clock
from app.core.prompt import PERSONA_PATH
from app.db.models import PersonaAmendment
from app.tg.send import answer_callback, edit_keyboard, send_keyboard, send_reply

logger = logging.getLogger(__name__)

REVOKE_LABEL = "Отозвать"
REVOKED_TEXT = "Отозвано."
STALE = "Устарело."

# How much of the amendment text a button label carries, matching
# app/tg/orders.py's own `[Снять] «{text[:24]}»` convention.
_LABEL_TEXT_MAX = 24


def render_amendments_list(rows: list[DisplayRow]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not rows:
        return amendments_module.EMPTY_LIST_TEXT, None
    lines = []
    buttons = []
    for item in rows:
        amendment = item.amendment
        suffix = f" {amendments_module.STALE_CHANGED}" if item.stale else ""
        lines.append(f"«{amendment.text}»{suffix}")
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{REVOKE_LABEL} «{amendment.text[:_LABEL_TEXT_MAX]}»",
                    callback_data=f"am:x:{amendment.id}",
                )
            ]
        )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def run_amendments_list(
    sessionmaker, bot: Bot, *, chat_id: int, persona_path: Path = PERSONA_PATH
) -> None:
    async with sessionmaker() as session:
        rows = await amendments_module.list_for_display(session, persona_path)
    text, markup = render_amendments_list(rows)
    await send_keyboard(bot, chat_id, text, markup)


async def handle_revoke_callback(
    sessionmaker,
    bot: Bot,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
    persona_path: Path = PERSONA_PATH,
) -> None:
    """`am:x:<id>` -- `/amendments`' own [Отозвать], re-rendering the list."""
    await answer_callback(bot, callback_id)
    _, _, raw_id = data.split(":", 2)

    try:
        amendment_id = int(raw_id)
    except ValueError:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        ok = await amendments_module.revoke(session, amendment_id, clock=clock)

    if not ok:
        await edit_keyboard(bot, chat_id, message_id, STALE, None)
        return

    async with sessionmaker() as session:
        rows = await amendments_module.list_for_display(session, persona_path)
    text, markup = render_amendments_list(rows)
    await edit_keyboard(bot, chat_id, message_id, text, markup)


async def send_trial_result(sessionmaker, bot: Bot, *, chat_id: int, amendment_id: int) -> None:
    """The `amendment_trial` job's result message (app/worker.py's own
    post-processing hook, the same shape as
    app/tg/orders.py's `send_order_proposal`)."""
    async with sessionmaker() as session:
        row = await session.get(PersonaAmendment, amendment_id)
    if row is None:
        return
    text = (
        amendments_module.ACTIVE_TEXT
        if row.status == amendments_module.ACTIVE
        else amendments_module.FAILED_TEXT
    )
    await send_reply(bot, chat_id, text)
