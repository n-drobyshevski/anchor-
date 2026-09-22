"""Entry point. MODE=webhook|polling.

Webhook mode: aiohttp app with three routes (webhook, healthz, readyz);
worker tasks start on_startup and are cancelled on_cleanup; the engine
is disposed on_cleanup. We do not use aiogram's SimpleRequestHandler —
it feeds the dispatcher inline, which contradicts plan section 6.1's
"enqueue and return 200 fast".

Polling mode: same worker, app.tg.polling.run_polling() instead of the
web app. Dev only.

Both modes run app.startup.run_startup_tasks() first (user_state
upsert + persona_version sync, plan section 5), then register bot
commands, before the worker/transport starts.

The LLM provider (1c) is constructed once here and threaded through
build_dispatcher() -> build_router(), so every turn shares one
AsyncOpenAI client/connection pool, and closed on shutdown in both
modes -- an unclosed client leaks its underlying HTTP connections.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiohttp import web

from app.config import Settings, check_runtime_settings, get_settings
from app.db.session import create_engine_and_sessionmaker, dispose_engine
from app.llm.openrouter import OpenRouterProvider
from app.llm.provider import LLMProvider
from app.log import setup_logging
from app.startup import run_startup_tasks
from app.tg.polling import run_polling
from app.tg.router import build_router, register_commands
from app.tg.webhook import handle_webhook, healthz, readyz
from app.worker import run_worker, stop_worker

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"


def build_provider(settings: Settings) -> LLMProvider:
    return OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL,
        max_tokens=settings.LLM_MAX_TOKENS,
        temperature=settings.LLM_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
    )


def build_dispatcher(sessionmaker, settings: Settings, provider: LLMProvider) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, provider))
    return dp


async def _on_startup(app: web.Application) -> None:
    settings: Settings = app["settings"]
    bot: Bot = app["bot"]
    sessionmaker = app["sessionmaker"]

    # Migrations already ran (the Railway start command is
    # `alembic upgrade head && python -m app.main`); this is the
    # "then upsert user_state... then persona_version" step that
    # follows, per plan section 5's last line.
    async with sessionmaker() as session:
        await run_startup_tasks(session, settings)

    await bot.set_webhook(
        url=settings.PUBLIC_URL.rstrip("/") + WEBHOOK_PATH,
        secret_token=settings.TELEGRAM_SECRET_TOKEN,
        allowed_updates=["message", "callback_query"],
    )
    await register_commands(bot)
    app["worker_tasks"] = await run_worker(sessionmaker, app["dp"], bot)
    logger.info("startup complete", extra={"event": "startup"})


async def _on_cleanup(app: web.Application) -> None:
    await stop_worker(app["worker_tasks"])
    await app["provider"].close()
    await dispose_engine(app["engine"])
    await app["bot"].session.close()
    logger.info("cleanup complete", extra={"event": "cleanup"})


def build_webhook_app(
    settings: Settings, bot: Bot, dp: Dispatcher, sessionmaker, engine, provider: LLMProvider
) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["bot"] = bot
    app["dp"] = dp
    app["sessionmaker"] = sessionmaker
    app["engine"] = engine
    app["provider"] = provider

    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def _run_polling_mode(
    settings: Settings, bot: Bot, dp: Dispatcher, sessionmaker, engine, provider: LLMProvider
) -> None:
    async with sessionmaker() as session:
        await run_startup_tasks(session, settings)

    await bot.delete_webhook(drop_pending_updates=False)
    await register_commands(bot)
    worker_tasks = await run_worker(sessionmaker, dp, bot)
    try:
        await run_polling(bot, sessionmaker, settings)
    finally:
        await stop_worker(worker_tasks)
        await provider.close()
        await dispose_engine(engine)
        await bot.session.close()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    # Before anything is constructed: Bot() and the LLM client both
    # reject an empty credential, and dying here kills the healthcheck.
    check_runtime_settings(settings)

    engine, sessionmaker = create_engine_and_sessionmaker(settings.DATABASE_URL)
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    provider = build_provider(settings)
    dp = build_dispatcher(sessionmaker, settings, provider)

    if settings.MODE == "webhook":
        app = build_webhook_app(settings, bot, dp, sessionmaker, engine, provider)
        web.run_app(app, host="0.0.0.0", port=settings.PORT)
    else:
        asyncio.run(_run_polling_mode(settings, bot, dp, sessionmaker, engine, provider))


if __name__ == "__main__":
    main()
