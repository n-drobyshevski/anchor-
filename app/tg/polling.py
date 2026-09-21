"""Dev-only transport: getUpdates long-poll loop -> the same enqueue path.

bot.get_updates() returns parsed aiogram Update objects, not raw wire
JSON, so we dump them back to a dict before storing. by_alias=True
matters: aiogram renames wire fields (e.g. "from" -> "from_user"), and a
dump without by_alias would produce a payload that only fails later, at
worker replay time. tests/test_polling.py guards the round trip.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.tg.webhook import filter_and_enqueue

logger = logging.getLogger(__name__)

POLL_TIMEOUT_SECONDS = 30


def dump_update(update: Update) -> dict:
    """The exact dump shape stored for a polled update; see module docstring."""
    return update.model_dump(mode="json", by_alias=True, exclude_none=True)


async def run_polling(
    bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    offset: int | None = None
    while True:
        updates = await bot.get_updates(
            offset=offset,
            timeout=POLL_TIMEOUT_SECONDS,
            allowed_updates=["message", "callback_query"],
        )
        for update in updates:
            offset = update.update_id + 1
            payload = dump_update(update)
            async with sessionmaker() as session:
                await filter_and_enqueue(session, payload, settings)
