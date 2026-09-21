"""Entry point. MODE=webhook|polling.

Webhook mode: aiohttp app with three routes (webhook, healthz, readyz);
worker tasks start on_startup and are cancelled on_cleanup; the engine
is disposed on_cleanup. We do not use aiogram's SimpleRequestHandler —
it feeds the dispatcher inline, which contradicts plan section 6.1's
"enqueue and return 200 fast".

Polling mode: same worker, app.tg.polling.run_polling() instead of the
web app. Dev only.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiohttp import web

from app.config import Settings, get_settings
from app.db.session import create_engine_and_sessionmaker, dispose_engine
from app.log import setup_logging
from app.tg.polling import run_polling
from app.tg.router import router as tg_router
from app.tg.webhook import handle_webhook, healthz, readyz
from app.worker import run_worker, stop_worker

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(tg_router)
    return dp


async def _on_startup(app: web.Application) -> None:
    settings: Settings = app["settings"]
    bot: Bot = app["bot"]
    await bot.set_webhook(
        url=settings.PUBLIC_URL.rstrip("/") + WEBHOOK_PATH,
        secret_token=settings.TELEGRAM_SECRET_TOKEN,
        allowed_updates=["message", "callback_query"],
    )
    app["worker_tasks"] = await run_worker(app["sessionmaker"], app["dp"], bot)
    logger.info("startup complete", extra={"event": "startup"})


async def _on_cleanup(app: web.Application) -> None:
    await stop_worker(app["worker_tasks"])
    await dispose_engine(app["engine"])
    await app["bot"].session.close()
    logger.info("cleanup complete", extra={"event": "cleanup"})


def build_webhook_app(settings: Settings, bot: Bot, dp: Dispatcher, sessionmaker, engine) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["bot"] = bot
    app["dp"] = dp
    app["sessionmaker"] = sessionmaker
    app["engine"] = engine

    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def _run_polling_mode(settings: Settings, bot: Bot, dp: Dispatcher, sessionmaker, engine) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    worker_tasks = await run_worker(sessionmaker, dp, bot)
    try:
        await run_polling(bot, sessionmaker, settings)
    finally:
        await stop_worker(worker_tasks)
        await dispose_engine(engine)
        await bot.session.close()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    engine, sessionmaker = create_engine_and_sessionmaker(settings.DATABASE_URL)
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = build_dispatcher()

    if settings.MODE == "webhook":
        app = build_webhook_app(settings, bot, dp, sessionmaker, engine)
        web.run_app(app, host="0.0.0.0", port=settings.PORT)
    else:
        asyncio.run(_run_polling_mode(settings, bot, dp, sessionmaker, engine))


if __name__ == "__main__":
    main()
