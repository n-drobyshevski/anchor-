"""`/digest [24h|7d]` and the idle undo callback (Phase 6 plan section 7;
approved plan §5).

Callback data is `idle:u:<run_id>`. `app/core/idle/digest.py` builds the
text and says which run ids are undo-eligible; this module owns the
Telegram side only -- sending, the keyboard, and answering the button.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.clock import Clock
from app.core.idle.digest import WINDOW_24H, WINDOW_7D, build_digest
from app.core.idle.undo import undo_run
from app.tg.send import answer_callback, send_keyboard, send_reply

logger = logging.getLogger(__name__)

DIGEST_USAGE = "Так: /digest, /digest 24h или /digest 7d."
UNDO_DONE = "Отменено."
UNDO_PARTIAL = "Часть изменений уже перезаписана."
UNDO_REFUSED = "Отменить нельзя."

UNDO_CALLBACK_PREFIX = "idle:u:"


def undo_callback_data(run_id: int) -> str:
    return f"{UNDO_CALLBACK_PREFIX}{run_id}"


def undo_keyboard(run_ids: tuple[int, ...]) -> InlineKeyboardMarkup | None:
    """One [Отменить] row per undoable run, or None if there is none."""
    if not run_ids:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отменить", callback_data=undo_callback_data(run_id))]
            for run_id in run_ids
        ]
    )


def parse_digest_args(raw: str | None) -> str | None:
    """`""`/`"24h"` -> WINDOW_24H, `"7d"` -> WINDOW_7D, anything else -> None."""
    text = (raw or "").strip().lower()
    if text in ("", WINDOW_24H):
        return WINDOW_24H
    if text == WINDOW_7D:
        return WINDOW_7D
    return None


async def run_digest(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    clock: Clock,
    *,
    chat_id: int,
    window: str,
) -> None:
    async with sessionmaker() as session:
        digest = await build_digest(
            session, clock, undo_days=settings.IDLE_UNDO_DAYS, window=window
        )
    markup = undo_keyboard(digest.undoable_run_ids)
    if markup is None:
        await send_reply(bot, chat_id, digest.text)
    else:
        await send_keyboard(bot, chat_id, digest.text, markup)


def _parse_run_id(data: str) -> int | None:
    if not data.startswith(UNDO_CALLBACK_PREFIX):
        return None
    tail = data[len(UNDO_CALLBACK_PREFIX):]
    return int(tail) if tail.isdigit() else None


async def handle_undo_callback(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """`idle:u:<run_id>` -- always answers, even on a refusal (plan
    section 8's convention every other callback in this codebase
    follows: an unanswered button spins in the client until it times
    out)."""
    run_id = _parse_run_id(data)
    if run_id is None:
        await answer_callback(bot, callback_id, UNDO_REFUSED)
        return

    async with sessionmaker() as session:
        result = await undo_run(session, settings, run_id, clock=clock)

    if result.status == "refused":
        await answer_callback(bot, callback_id, UNDO_REFUSED)
        return

    text = UNDO_DONE if result.skipped_conflicts == 0 else UNDO_PARTIAL
    await answer_callback(bot, callback_id, text)
    logger.info(
        "idle run undone via callback",
        extra={"run_id": run_id, "restored": result.restored, "conflicts": result.skipped_conflicts},
    )


__all__ = [
    "DIGEST_USAGE",
    "UNDO_CALLBACK_PREFIX",
    "UNDO_DONE",
    "UNDO_PARTIAL",
    "UNDO_REFUSED",
    "handle_undo_callback",
    "parse_digest_args",
    "run_digest",
    "undo_callback_data",
    "undo_keyboard",
]
