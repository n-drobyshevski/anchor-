"""Package B: dark features really are dark when their flag is off.

Each test goes through the production entry point itself --
app/main.py's `build_webhook_app`, the real idle facts query, the real
Settings defaults -- not a copy of its `if` line. The per-feature
command and job refusals live next to their own features' tests
(test_research_commands, test_research_jobs, test_planner_commands,
test_grok_access, test_backup); this file holds what had no home there.
"""

from __future__ import annotations

import datetime

from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.main import build_webhook_app

ORIGIN = "https://anchor.example.test"


def _settings(**overrides) -> Settings:
    base = dict(MODE="webhook", TELEGRAM_BOT_TOKEN="123456:TEST", PUBLIC_URL=ORIGIN)
    base.update(overrides)
    return Settings(**base)


def _app(settings: Settings, sessionmaker):
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    app = build_webhook_app(
        settings, bot, Dispatcher(), sessionmaker,
        engine=None, provider=None, cheap_provider=None, safety_provider=None,
        llm_client=None, clock=None, hub=None, code_store=None,
    )
    return app, bot


def _paths(app) -> set[str]:
    return {
        route.resource.canonical
        for route in app.router.routes()
        if route.resource is not None
    }


async def test_default_flags_register_only_the_webhook_health_and_oauth_routes(sessionmaker):
    settings = _settings()
    assert not settings.GROK_ACCESS_ENABLED
    assert not settings.WEB_UI_ENABLED
    assert not settings.PLANNER_ENABLED
    assert not settings.RESEARCH_ENABLED
    app, bot = _app(settings, sessionmaker)

    paths = _paths(app)

    # No MCP capability route, no web-chat API, no page.
    assert not any(path.startswith("/mcp") for path in paths)
    assert not any(path.startswith("/api") for path in paths)
    assert "/" not in paths
    assert paths == {
        "/telegram/webhook",
        "/healthz",
        "/readyz",
        "/planner/oauth/callback",
    }
    await bot.session.close()


async def test_planner_oauth_callback_404s_while_the_planner_is_off(sessionmaker):
    app, bot = _app(_settings(), sessionmaker)
    # on_startup would start the worker and the webhook; this exercises
    # routing only.
    app.on_startup.clear()
    app.on_cleanup.clear()
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/planner/oauth/callback?code=x&state=y")
        assert response.status == 404
    await bot.session.close()


async def test_grok_route_exists_only_when_enabled(sessionmaker):
    app, bot = _app(_settings(GROK_ACCESS_ENABLED=True), sessionmaker)
    assert any(path.startswith("/mcp") for path in _paths(app))
    await bot.session.close()


async def test_idle_gate_says_paused_when_persona_is_off(sessionmaker, frozen_clock):
    """The real facts query, not hand-built facts: persona_active=false
    on the user_state row reaches the gate as `paused`."""
    from app.core.idle import facts as idle_facts
    from app.core.idle import gate as idle_gate
    from app.db.models import UserState

    clock = frozen_clock(2026, 9, 25, 3, 0, tz="Europe/Paris")
    async with sessionmaker() as session:
        session.add(
            UserState(
                id=1, chat_id=1, timezone="Europe/Paris", persona_active=False,
                last_user_msg_at=clock.now_utc() - datetime.timedelta(hours=10),
            )
        )
        await session.commit()

    from app.core.idle import KINDS

    settings = Settings(IDLE_ENABLED=True)
    async with sessionmaker() as session:
        facts = await idle_facts.load_idle_facts(session, settings, clock, "Europe/Paris")
    config = idle_gate.config_from_settings(settings)

    for kind in KINDS:
        result = idle_gate.idle_gate(kind, facts, clock.now_utc(), config)
        assert result.allowed is False, kind
        assert result.reason == idle_gate.PAUSED == "paused", kind


def test_default_judge_is_independent_so_eval_never_exits_3():
    """eval/run.py refuses (exit 3) only when the judge equals LLM_MODEL.
    The defaults must not trip it."""
    from eval.run import EXIT_SAME_JUDGE, judge_model_for, same_judge_warning

    settings = Settings()
    assert settings.LLM_MODEL_JUDGE == "openai/gpt-4.1-nano"
    assert settings.LLM_MODEL == "thedrummer/cydonia-24b-v4.1"
    judge = judge_model_for(settings)
    assert judge == "openai/gpt-4.1-nano"
    assert same_judge_warning(judge, settings) is None
    assert EXIT_SAME_JUDGE == 3
