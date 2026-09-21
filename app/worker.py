"""Single-concurrency worker: claim -> feed_update -> complete/fail.

Concurrency is 1, which preserves message order for a single user
(plan section 6.3). recover_stuck runs in its own task every 60s —
folding it into the claim loop would starve it whenever the queue stays
busy.

Transaction boundary: claim() commits (releasing the FOR UPDATE row
lock) before feed_update runs. A Postgres lock is never held across a
Telegram API call.

Privacy: on failure only type(exc).__name__ and update_id are logged or
stored as the queue row's error — never exception args, which could
carry payload content.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.queue import claim, complete, fail, recover_stuck

logger = logging.getLogger(__name__)

IDLE_SLEEP_SECONDS = 0.5
RECOVER_INTERVAL_SECONDS = 60


async def process_one_update(
    sessionmaker: async_sessionmaker[AsyncSession], dp: Dispatcher, bot: Bot
) -> bool:
    """Claim and process a single update. Returns True iff a row was claimed.

    Extracted from the claim loop so tests can drive exactly one cycle
    without running an infinite loop.
    """
    async with sessionmaker() as session:
        row = await claim(session)

    if row is None:
        return False

    started_at = time.monotonic()
    try:
        update = Update.model_validate(row.payload, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
        async with sessionmaker() as session:
            await fail(session, row.update_id, type(exc).__name__)
        logger.warning(
            "update processing failed",
            extra={
                "update_id": row.update_id,
                "event": type(exc).__name__,
                "latency_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
    else:
        async with sessionmaker() as session:
            await complete(session, row.update_id)
        logger.info(
            "update processed",
            extra={
                "update_id": row.update_id,
                "latency_ms": int((time.monotonic() - started_at) * 1000),
            },
        )

    return True


async def _claim_loop(
    sessionmaker: async_sessionmaker[AsyncSession], dp: Dispatcher, bot: Bot
) -> None:
    while True:
        processed = await process_one_update(sessionmaker, dp, bot)
        if not processed:
            await asyncio.sleep(IDLE_SLEEP_SECONDS)


async def _recover_loop(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    while True:
        await asyncio.sleep(RECOVER_INTERVAL_SECONDS)
        async with sessionmaker() as session:
            recovered = await recover_stuck(session)
        if recovered:
            logger.info("recovered stuck rows", extra={"count": recovered})


async def run_worker(
    sessionmaker: async_sessionmaker[AsyncSession], dp: Dispatcher, bot: Bot
) -> list[asyncio.Task]:
    """Start the claim loop and the recovery sweep as two background tasks."""
    claim_task = asyncio.create_task(_claim_loop(sessionmaker, dp, bot), name="anchor-claim-loop")
    recover_task = asyncio.create_task(_recover_loop(sessionmaker), name="anchor-recover-loop")
    return [claim_task, recover_task]


async def stop_worker(tasks: list[asyncio.Task]) -> None:
    """Cancel and await the worker's background tasks (shutdown path)."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
