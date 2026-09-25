"""app/main.py's web-chat shutdown wiring (low-severity finding: "No
on_shutdown hook closes the hub").

A live SSE stream otherwise holds up AppRunner.cleanup's
server.shutdown(...) for the full shutdown_timeout on every deploy,
because nothing ever told the stream's handler to stop waiting.
build_webhook_app() now registers an on_shutdown hook (only when the
web UI is enabled) that calls WebHub.close_all() -- this exercises that
hook the same way aiohttp itself would, through Application.shutdown(),
without needing the full worker/bot/engine lifecycle on_startup and
on_cleanup pull in.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher

from app.config import Settings
from app.main import build_webhook_app
from app.web import auth
from app.web.hub import WebHub

ORIGIN = "https://anchor.example.test"


def _settings(**overrides) -> Settings:
    base = dict(
        MODE="webhook",
        TELEGRAM_BOT_TOKEN="123456:TEST",
        PUBLIC_URL=ORIGIN,
        WEB_UI_ENABLED=True,
    )
    base.update(overrides)
    return Settings(**base)


async def test_shutdown_closes_the_hub_when_web_ui_is_enabled(sessionmaker):
    settings = _settings()
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    hub = WebHub()
    code_store = auth.CodeStore()
    app = build_webhook_app(
        settings,
        bot,
        dp,
        sessionmaker,
        engine=None,
        provider=None,
        cheap_provider=None,
        safety_provider=None,
        llm_client=None,
        clock=None,
        hub=hub,
        code_store=code_store,
    )
    sub = hub.subscribe()

    app.freeze()  # aiohttp requires this before a signal may fire
    await app.shutdown()  # fires on_shutdown only, not on_cleanup

    events = [record async for record in sub.events()]
    assert events == []  # the poison pill closed the stream

    await bot.session.close()


async def test_shutdown_does_nothing_web_related_when_disabled(sessionmaker):
    settings = _settings(WEB_UI_ENABLED=False)
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    app = build_webhook_app(
        settings,
        bot,
        dp,
        sessionmaker,
        engine=None,
        provider=None,
        cheap_provider=None,
        safety_provider=None,
        llm_client=None,
        clock=None,
        hub=None,
        code_store=None,
    )

    app.freeze()
    await app.shutdown()  # must not raise (no web_hub key at all)

    await bot.session.close()
