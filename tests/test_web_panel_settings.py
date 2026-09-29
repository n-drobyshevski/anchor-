"""app/web/panels/settings.py end to end: Настройки's integrations and
the idle digest.

Covers: 401/403/429; GET /api/settings with every integration off (all
null) and on, never exposing a token; /vault notes on|off through
app/vault/consent.py (off deletes the note rows); /planner on|off
through set_enabled, and 409 before the planner was ever linked; the
integrations' 404 while their deploy switch is off; that nothing here
writes Claude's library switches; GET /api/digest (/digest's own text
plus the undoable runs, labelled) and POST /api/digest/{id}/undo;
invalidate("settings"); silence in Telegram.
"""

from __future__ import annotations

import datetime
import json
import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import IdleRun, PlannerCredential, UserState, VaultFile
from app.web.hub import WebHub
from app.web.ratelimit import WebRateLimiter
from app.web.routes import setup_web
from app.web.sink import make_web_bot
from conftest import make_bot
from scripts.web_passphrase import make_hash

pytestmark = pytest.mark.asyncio

CHAT_ID = 4242
PASSPHRASE = "correct horse battery staple"
HASH = make_hash(PASSPHRASE)
ORIGIN = "https://anchor.example.test"
TIMEZONE = "Europe/Paris"
START = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)

API_HEADERS = {"Sec-Fetch-Site": "same-origin", "Origin": ORIGIN}
JSON_HEADERS = {**API_HEADERS, "Content-Type": "application/json"}


def _settings(**overrides) -> Settings:
    base = dict(
        ALLOWED_CHAT_ID=CHAT_ID,
        PUBLIC_URL=ORIGIN,
        WEB_PASSPHRASE_HASH=HASH,
        WEB_SESSION_IDLE_HOURS=72,
        WEB_SESSION_MAX_DAYS=14,
        WEB_LOGIN_CODE_TTL_S=300,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, sessionmaker, clock, bot, hub=None):
    app = web.Application()
    app["settings"] = settings
    app["sessionmaker"] = sessionmaker
    app["clock"] = clock
    app["bot"] = bot
    hub = hub or WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    setup_web(app, hub=hub, web_bot=web_bot)
    return app, hub, web_bot


def _cookie(resp, name: str) -> str:
    return resp.cookies[name].value


async def _post(client, path, body, cookies: dict | None = None, headers=None):
    hdrs = dict(headers or JSON_HEADERS)
    if cookies:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return await client.post(path, headers=hdrs, data=json.dumps(body))


async def _get(client, path, cookies: dict | None = None, headers=None):
    hdrs = dict(headers or API_HEADERS)
    if cookies:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return await client.get(path, headers=hdrs)


async def _log_in(client, fake_session) -> dict:
    resp = await _post(client, "/api/auth/passphrase", {"passphrase": PASSPHRASE})
    assert resp.status == 200
    pre_token = _cookie(resp, "__Host-anchor_pre")
    code = re.search(r"[0-9A-Z]{4}-[0-9A-Z]{4}", fake_session.sent[-1].text).group(0)
    resp = await _post(
        client, "/api/auth/code", {"code": code}, cookies={"__Host-anchor_pre": pre_token}
    )
    assert resp.status == 200
    return {"__Host-anchor_s": _cookie(resp, "__Host-anchor_s")}


async def _seed_state(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE))
        await session.commit()



def _topics(hub: WebHub) -> list[str]:
    return [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]


ON = dict(VAULT_MODE="status", VAULT_API_TOKEN="t" * 32, PLANNER_ENABLED=True, CLAUDE_ACCESS_ENABLED=True)


async def _app(sessionmaker, **overrides):
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(**overrides), sessionmaker, FrozenClock(START), bot)
    return app, hub, fake


async def _credential(sessionmaker, *, enabled: bool = True) -> None:
    async with sessionmaker() as session:
        session.add(
            PlannerCredential(
                id=1, access_token="SECRET-ACCESS", refresh_token="SECRET-REFRESH",
                expires_at=START + datetime.timedelta(hours=1), status="active", enabled=enabled,
            )
        )
        await session.commit()


async def test_settings_require_a_session(sessionmaker):
    app, _hub, _fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        assert (await _get(client, "/api/settings")).status == 401
        assert (await _post(client, "/api/settings/notes", {"on": True})).status == 401
        assert (await _post(client, "/api/settings/planner", {"on": True})).status == 401
        assert (await _get(client, "/api/digest")).status == 401
        assert (await _post(client, "/api/digest/1/undo", {})).status == 401


async def test_settings_reject_a_foreign_origin(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(
            client, "/api/settings", cookies=cookies,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert resp.status == 403


async def test_settings_with_everything_off(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/settings", cookies=cookies)).json()
        assert (await _post(client, "/api/settings/notes", {"on": True}, cookies=cookies)).status == 404
        assert (await _post(client, "/api/settings/planner", {"on": True}, cookies=cookies)).status == 404
    assert body["vault"] is None and body["planner"] is None and body["claude"] is None
    assert body["idle"]["undo_days"] == 7


async def test_settings_with_everything_on_never_shows_a_token(sessionmaker):
    await _seed_state(sessionmaker)
    await _credential(sessionmaker, enabled=False)
    app, _hub, fake = await _app(sessionmaker, **ON)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/settings", cookies=cookies)
        raw = await resp.text()
        body = json.loads(raw)
    assert body["vault"]["mode"] == "status" and body["vault"]["notes_consent"] is False
    assert body["planner"] == {"linked": True, "status": "active", "enabled": False}
    assert body["claude"] == {"connected": False, "expires_at": None, "library_read": False, "library_write": False}
    assert "SECRET" not in raw


async def test_notes_on_then_off_deletes_note_rows(sessionmaker):
    await _seed_state(sessionmaker)
    app, hub, fake = await _app(sessionmaker, **ON)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        sent_before = len(fake.sent)
        resp = await _post(client, "/api/settings/notes", {"on": True}, cookies=cookies)
        assert resp.status == 200
        assert (await resp.json())["settings"]["vault"]["notes_consent"] is True
        async with sessionmaker() as session:
            session.add(VaultFile(path="Notes/a.md", role="note", note_class="knowledge"))
            await session.commit()
        resp = await _post(client, "/api/settings/notes", {"on": False}, cookies=cookies)
        assert resp.status == 200
        assert len(fake.sent) == sent_before

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        notes = list((await session.execute(select(VaultFile).where(VaultFile.role == "note"))).scalars())
    assert state.notes_consent is False
    assert notes == []
    assert "settings" in _topics(hub)


async def test_notes_rejects_a_non_bool(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker, **ON)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        assert (await _post(client, "/api/settings/notes", {"on": "yes"}, cookies=cookies)).status == 400


async def test_planner_switch(sessionmaker):
    await _seed_state(sessionmaker)
    await _credential(sessionmaker, enabled=True)
    app, hub, fake = await _app(sessionmaker, **ON)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/settings/planner", {"on": False}, cookies=cookies)
        assert resp.status == 200
        assert (await resp.json())["settings"]["planner"]["enabled"] is False
    async with sessionmaker() as session:
        assert (await session.get(PlannerCredential, 1)).enabled is False
    assert "settings" in _topics(hub)


async def test_planner_switch_before_linking_is_409(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker, **ON)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/settings/planner", {"on": True}, cookies=cookies)
        assert resp.status == 409
        assert (await resp.json())["error"] == "not_linked"


async def test_no_endpoint_writes_claudes_library_switches(sessionmaker):
    """Claude's library switches stay Telegram-only (app/tg/menu.py's
    action_available refuses them for the web): nothing is routed here."""
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker, **ON)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for path in ("/api/settings/claude", "/api/settings/library", "/api/settings/claude/library"):
            assert (await _post(client, path, {"on": True}, cookies=cookies)).status in (404, 405)


async def test_digest_lists_undoable_runs_and_undo_works(sessionmaker):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        run = IdleRun(
            kind="consolidate", local_date=START.date(), status="done", reversible=True,
            summary={"merged": 2, "contradicted": 1},
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    app, hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/digest?window=7d", cookies=cookies)).json()
        assert body["window"] == "7d"
        assert body["lines"][0].startswith("Фоновая работа за 7 дн.")
        assert [(u["id"], u["label"]) for u in body["undoable"]] == [
            (run_id, "Память: объединено 2, противоречий 1")
        ]
        sent_before = len(fake.sent)
        resp = await _post(client, f"/api/digest/{run_id}/undo", {}, cookies=cookies)
        assert resp.status == 200
        again = await _post(client, f"/api/digest/{run_id}/undo", {}, cookies=cookies)
        assert again.status == 409
        assert (await again.json())["reason"]
        assert len(fake.sent) == sent_before
        after = await (await _get(client, "/api/digest?window=7d", cookies=cookies)).json()
    assert after["undoable"] == []
    assert {"settings", "memory", "notebook"} <= set(_topics(hub))


async def test_digest_rejects_an_unknown_window(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        assert (await _get(client, "/api/digest?window=30d", cookies=cookies)).status == 400
        body = await (await _get(client, "/api/digest", cookies=cookies)).json()
    assert body["lines"] == ["Фоновой работы не было."]


async def test_undo_unknown_run(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        assert (await _post(client, "/api/digest/999999/undo", {}, cookies=cookies)).status == 409
        assert (await _post(client, "/api/digest/abc/undo", {}, cookies=cookies)).status == 404


async def test_settings_writes_rate_limited(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app(sessionmaker, **ON)
    limiter: WebRateLimiter = app["web_rate_limiter"]
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for _ in range(60):
            assert limiter.check_panel_write() is None
        assert (await _post(client, "/api/settings/notes", {"on": True}, cookies=cookies)).status == 429
