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

3a builds the one SystemClock here and threads it the same way
(phase-3 plan section 3). One instance, injected into the dispatcher
for the handler path and into the worker for the job path, so nothing
under app/core/ ever reads the wall clock for itself and a test can
substitute a FrozenClock at either entry point.

2a adds a second provider for background work (scene summaries now;
the extractor and the welfare classifier later). It runs the same model
as chat today, by decision, but is a separate LLMProvider because its
max_tokens and temperature differ and because compute_cost is
model-aware. Both share one AsyncOpenAI client, built here and closed
here: neither provider owns it (see app/llm/openrouter.py), so shutdown
closes the client directly rather than through either one.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import sys

from aiogram import Bot, Dispatcher
from aiohttp import web

from app.config import Settings, check_runtime_settings, get_settings
from app.core.clock import Clock, SystemClock
from app.db.session import create_engine_and_sessionmaker, dispose_engine
from app.llm.openrouter import OpenRouterProvider, build_client
from app.llm.provider import LLMProvider
from app.log import setup_logging
from app.startup import run_startup_tasks
from app.tg.polling import run_polling
from app.tg.router import build_router, register_commands
from app.tg.webhook import handle_webhook, healthz, readyz
from app.worker import run_worker, stop_worker

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"


def build_providers(settings: Settings):
    """The chat, background and safety providers, and the client they share.

    Returns (provider, cheap_provider, safety_provider, client). The
    caller owns `client` and must close it on shutdown; calling close()
    on any of the three providers is a no-op, by design
    (app/llm/openrouter.py).

    Three rather than two since H2. The split is not about cost -- the
    safety model is cheaper than the one it replaced -- but about what
    each call is for. `provider` and `cheap_provider` produce prose; the
    safety provider produces strict JSON verdicts that decide whether
    the persona speaks at all, and it is the only one whose model was
    chosen for schema compliance instead of voice.
    """
    client = build_client(settings.OPENROUTER_API_KEY)
    provider = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL,
        max_tokens=settings.LLM_MAX_TOKENS,
        temperature=settings.LLM_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        client=client,
        # No structured_outputs here on purpose: no main-model call ever
        # passes a json_schema, so the constructor default is unreachable
        # rather than merely unset. Said out loud because H2 exists
        # partly to remove assumptions that were only ever implicit.
    )
    cheap_provider = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL_CHEAP,
        max_tokens=settings.LLM_CHEAP_MAX_TOKENS,
        temperature=settings.LLM_CHEAP_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        client=client,
        structured_outputs=settings.LLM_STRUCTURED_OUTPUTS,
    )
    safety_provider = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL_SAFETY,
        max_tokens=settings.LLM_SAFETY_MAX_TOKENS,
        temperature=settings.LLM_SAFETY_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        client=client,
        structured_outputs=settings.LLM_STRUCTURED_OUTPUTS,
    )
    return provider, cheap_provider, safety_provider, client


def build_dispatcher(
    sessionmaker,
    settings: Settings,
    provider: LLMProvider,
    safety_provider: LLMProvider | None = None,
    clock: Clock | None = None,
) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, settings, provider, safety_provider, clock or SystemClock())
    )
    return dp


async def _on_startup(app: web.Application) -> None:
    settings: Settings = app["settings"]
    bot: Bot = app["bot"]
    sessionmaker = app["sessionmaker"]

    # Migrations already ran (the Railway start command is
    # `alembic upgrade head && python -m app.main`); this is the
    # "then upsert user_state... then persona_version" step that
    # follows, per plan section 5's last line.
    logger.info("startup step", extra={"event": "startup_tasks"})
    async with sessionmaker() as session:
        await run_startup_tasks(session, settings)

    logger.info("startup step", extra={"event": "set_webhook"})
    await bot.set_webhook(
        url=settings.PUBLIC_URL.rstrip("/") + WEBHOOK_PATH,
        secret_token=settings.TELEGRAM_SECRET_TOKEN,
        allowed_updates=["message", "callback_query"],
    )
    logger.info("startup step", extra={"event": "register_commands"})
    await register_commands(bot)
    logger.info("startup step", extra={"event": "run_worker"})
    app["worker_tasks"] = await run_worker(
        sessionmaker,
        app["dp"],
        bot,
        settings,
        app["cheap_provider"],
        app["clock"],
        app["provider"],
        app["safety_provider"],
    )
    faulthandler.cancel_dump_traceback_later()
    logger.info("startup complete", extra={"event": "startup"})


async def _on_cleanup(app: web.Application) -> None:
    await stop_worker(app["worker_tasks"])
    await app["llm_client"].close()
    await dispose_engine(app["engine"])
    await app["bot"].session.close()
    logger.info("cleanup complete", extra={"event": "cleanup"})


def build_webhook_app(
    settings: Settings,
    bot: Bot,
    dp: Dispatcher,
    sessionmaker,
    engine,
    provider: LLMProvider,
    cheap_provider: LLMProvider,
    safety_provider: LLMProvider,
    llm_client,
    clock: Clock,
) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["bot"] = bot
    app["dp"] = dp
    app["sessionmaker"] = sessionmaker
    app["engine"] = engine
    app["provider"] = provider
    app["cheap_provider"] = cheap_provider
    app["safety_provider"] = safety_provider
    app["llm_client"] = llm_client
    app["clock"] = clock

    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def _run_polling_mode(
    settings: Settings,
    bot: Bot,
    dp: Dispatcher,
    sessionmaker,
    engine,
    cheap_provider: LLMProvider,
    safety_provider: LLMProvider,
    llm_client,
    clock: Clock,
    provider: LLMProvider | None = None,
) -> None:
    async with sessionmaker() as session:
        await run_startup_tasks(session, settings)

    await bot.delete_webhook(drop_pending_updates=False)
    await register_commands(bot)
    worker_tasks = await run_worker(
        sessionmaker, dp, bot, settings, cheap_provider, clock, provider, safety_provider
    )
    faulthandler.cancel_dump_traceback_later()
    try:
        await run_polling(bot, sessionmaker, settings)
    finally:
        await stop_worker(worker_tasks)
        await llm_client.close()
        await dispose_engine(engine)
        await bot.session.close()


# If startup has not finished by then, dump every thread's stack to
# stderr (and keep dumping) so a hang is visible in the deploy log
# instead of a silent healthcheck timeout. Cancelled in _on_startup.
STARTUP_TRACE_AFTER_S = 90


def main() -> None:
    # stderr, unbuffered, before anything that could hang: proves the
    # process got past `alembic upgrade head &&` in the start command.
    print("app.main: starting", file=sys.stderr, flush=True)
    faulthandler.enable(file=sys.stderr)
    faulthandler.dump_traceback_later(STARTUP_TRACE_AFTER_S, repeat=True, file=sys.stderr)
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    # Before anything is constructed: Bot() and the LLM client both
    # reject an empty credential, and dying here kills the healthcheck.
    check_runtime_settings(settings)

    engine, sessionmaker = create_engine_and_sessionmaker(settings.DATABASE_URL)
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    provider, cheap_provider, safety_provider, llm_client = build_providers(settings)
    clock = SystemClock()
    dp = build_dispatcher(sessionmaker, settings, provider, safety_provider, clock)

    if settings.MODE == "webhook":
        app = build_webhook_app(
            settings,
            bot,
            dp,
            sessionmaker,
            engine,
            provider,
            cheap_provider,
            safety_provider,
            llm_client,
            clock,
        )
        web.run_app(app, host="0.0.0.0", port=settings.PORT)
    else:
        asyncio.run(
            _run_polling_mode(
                settings,
                bot,
                dp,
                sessionmaker,
                engine,
                cheap_provider,
                safety_provider,
                llm_client,
                clock,
                provider,
            )
        )


if __name__ == "__main__":
    main()
