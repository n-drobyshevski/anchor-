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
import logging

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
from app.web import auth as web_auth
from app.web.hub import WebHub
from app.web.routes import setup_web
from app.web.sink import make_web_bot
from app.web.tail import start_tail, stop_tail
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
    hub: WebHub | None = None,
    code_store: web_auth.CodeStore | None = None,
) -> Dispatcher:
    """`hub` and `code_store` (web-chat plan track 2) default to None so
    every caller and test predating the web UI keeps its shorter call;
    both are only passed when WEB_UI_ENABLED, and both thread through to
    build_router()'s `/weblogout` handler -- the one command that needs
    to reach them from inside the ordinary Telegram-side dispatcher.
    """
    dp = Dispatcher()
    dp.include_router(
        build_router(
            sessionmaker,
            settings,
            provider,
            safety_provider,
            clock or SystemClock(),
            hub,
            code_store,
        )
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
    async with sessionmaker() as session:
        await run_startup_tasks(session, settings)

    await bot.set_webhook(
        url=settings.PUBLIC_URL.rstrip("/") + WEBHOOK_PATH,
        secret_token=settings.TELEGRAM_SECRET_TOKEN,
        allowed_updates=["message", "callback_query"],
    )
    await register_commands(bot, web_ui_enabled=settings.WEB_UI_ENABLED)

    # Web-chat plan track 2: started only when WEB_UI_ENABLED, after the
    # worker knows about web_bot but before anything can race it -- the
    # tail task's cursor is read at start_tail() time (app/web/tail.py),
    # so it must not start before build_webhook_app has already wired
    # everything else into `app`.
    web_bot = app.get("web_bot")
    if web_bot is not None:
        app["web_tail_task"] = await start_tail(sessionmaker, app["web_hub"])

    app["worker_tasks"] = await run_worker(
        sessionmaker,
        app["dp"],
        bot,
        settings,
        app["cheap_provider"],
        app["clock"],
        app["provider"],
        app["safety_provider"],
        web_bot,
    )
    logger.info("startup complete", extra={"event": "startup"})


async def _on_cleanup(app: web.Application) -> None:
    await stop_worker(app["worker_tasks"])
    web_tail_task = app.get("web_tail_task")
    if web_tail_task is not None:
        await stop_tail(web_tail_task)
    web_bot = app.get("web_bot")
    if web_bot is not None:
        # WebSinkSession.close() is a no-op (it never opens a real HTTP
        # connection), but this Bot is a resource app/main.py created
        # and owns, exactly like the real one two lines below -- an
        # unclosed session is an unclosed session regardless of what its
        # close() actually does.
        await web_bot.session.close()
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
    hub: WebHub | None = None,
    code_store: web_auth.CodeStore | None = None,
) -> web.Application:
    """`hub` (web-chat plan track 2) is passed in, not built here, so the
    same WebHub instance `main()` handed to `build_dispatcher()` (for
    `/weblogout`) is the one `setup_web()` wires SSE subscribers into --
    two hubs would mean `/weblogout` could close streams `GET /api/events`
    never subscribed to. Non-None here is exactly the WEB_UI_ENABLED
    signal: main() only ever constructs and passes one when it is set,
    so this function needs no separate settings check of its own to
    decide whether to call setup_web(). `code_store` gets the same
    share-one-instance treatment for the same reason: /weblogout's
    kill switch (app/tg/router.py) must invalidate the very CodeStore
    POST /api/auth/passphrase issues codes into, not a second, empty one.
    """
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

    if hub is not None:
        web_bot = make_web_bot(settings.TELEGRAM_BOT_TOKEN, hub)
        app["web_hub"] = hub
        app["web_bot"] = web_bot
        setup_web(app, hub=hub, web_bot=web_bot, code_store=code_store)
        # A live SSE stream (GET /api/events) otherwise holds up
        # AppRunner.cleanup's server.shutdown(...) for the full 60s
        # shutdown_timeout on every deploy: hub.close_all() sends every
        # open stream its poison pill up front, so routes.events' own
        # loop ends on its next iteration instead of needing to be
        # force-cancelled (a low-severity finding: "No on_shutdown hook
        # closes the hub"). on_shutdown runs before on_cleanup, so this
        # fires well before _on_cleanup below tears down the worker/bots.
        app.on_shutdown.append(_on_web_shutdown)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def _on_web_shutdown(app: web.Application) -> None:
    app["web_hub"].close_all()


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
    try:
        await run_polling(bot, sessionmaker, settings)
    finally:
        await stop_worker(worker_tasks)
        await llm_client.close()
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
    provider, cheap_provider, safety_provider, llm_client = build_providers(settings)
    clock = SystemClock()
    # Web-chat plan track 2: one WebHub per process, built here (never
    # inside build_webhook_app) so build_dispatcher()'s /weblogout
    # handler and build_webhook_app()'s setup_web() share the exact same
    # instance -- see build_webhook_app's docstring. None when disabled,
    # which is the single signal both functions key off of.
    hub = WebHub() if settings.WEB_UI_ENABLED else None
    # Built alongside `hub`, for the same reason: build_dispatcher()'s
    # /weblogout handler and build_webhook_app()'s setup_web() must
    # share this exact CodeStore instance, not one each (see
    # build_webhook_app's docstring).
    code_store = web_auth.CodeStore() if settings.WEB_UI_ENABLED else None
    dp = build_dispatcher(sessionmaker, settings, provider, safety_provider, clock, hub, code_store)

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
            hub,
            code_store,
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
