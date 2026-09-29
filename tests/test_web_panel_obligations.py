"""app/web/panels/obligations.py end to end: GET /api/obligations and
POST /api/obligations/{id}/done|drop (the debt queue on Сегодня).

Covers: 401/403/429, the open list (oldest first, closed rows left
out), closing as done or dropped through app/core/obligations.py, the
404 for an unknown, malformed or already-closed id, that a close is
silent in Telegram and publishes invalidate("debts"), and that a
debt's text never reaches a log line.
"""

from __future__ import annotations

import json
import logging
import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.core import obligations
from app.core.clock import FrozenClock
from app.db.models import Obligation, UserState
from app.web.hub import WebHub
from app.web.ratelimit import WebRateLimiter
from app.web.routes import setup_web
from app.web.sink import make_web_bot
from conftest import make_bot
from scripts.web_passphrase import make_hash

import datetime

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


async def _open(sessionmaker, text: str, kind: str = "promised") -> int:
    async with sessionmaker() as session:
        row = await obligations.open_(session, text=text, kind=kind, source="proposal")
        return row.id


def _topics(hub: WebHub) -> list[str]:
    return [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]


# --- GET /api/obligations ------------------------------------------------


async def test_obligations_requires_a_session(sessionmaker):
    bot, _fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _get(client, "/api/obligations")
        assert resp.status == 401
        resp = await _post(client, "/api/obligations/1/done", {})
        assert resp.status == 401


async def test_obligations_rejects_a_foreign_origin(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(
            client,
            "/api/obligations",
            cookies=cookies,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert resp.status == 403


async def test_get_obligations_lists_open_debts_oldest_first(sessionmaker, clock):
    await _seed_state(sessionmaker)
    first = await _open(sessionmaker, "позвонить маме")
    second = await _open(sessionmaker, "сдать отчёт", kind="focus")
    closed = await _open(sessionmaker, "уже сделано")
    async with sessionmaker() as session:
        await obligations.close(session, clock, closed)

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/obligations", cookies=cookies)
        assert resp.status == 200
        body = await resp.json()

    assert [item["id"] for item in body["items"]] == [first, second]
    assert body["items"][0]["text"] == "позвонить маме"
    assert body["items"][1]["kind"] == "focus"
    assert body["items"][0]["due_local_date"] is None
    assert body["max_open"] == obligations.MAX_OPEN


async def test_get_obligations_empty(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/obligations", cookies=cookies)
        assert (await resp.json())["items"] == []


# --- POST done/drop --------------------------------------------------------


@pytest.mark.parametrize(("action", "status"), [("done", "done"), ("drop", "dropped")])
async def test_close_marks_the_debt_and_is_silent(sessionmaker, action, status):
    await _seed_state(sessionmaker)
    oid = await _open(sessionmaker, "позвонить маме")

    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        sent_before = len(fake.sent)
        resp = await _post(client, f"/api/obligations/{oid}/{action}", {}, cookies=cookies)
        assert resp.status == 200
        assert (await resp.json())["obligation"] == {"id": oid, "status": status}

    async with sessionmaker() as session:
        row = await session.get(Obligation, oid)
    assert row.status == status
    assert row.closed_at is not None
    assert len(fake.sent) == sent_before
    assert "debts" in _topics(hub)


async def test_close_twice_returns_404_the_second_time(sessionmaker):
    await _seed_state(sessionmaker)
    oid = await _open(sessionmaker, "позвонить маме")

    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        assert (await _post(client, f"/api/obligations/{oid}/done", {}, cookies=cookies)).status == 200
        topics_after_first = len(_topics(hub))
        resp = await _post(client, f"/api/obligations/{oid}/drop", {}, cookies=cookies)
        assert resp.status == 404

    async with sessionmaker() as session:
        row = await session.get(Obligation, oid)
    assert row.status == "done"  # the second press changed nothing
    assert len(_topics(hub)) == topics_after_first


@pytest.mark.parametrize("raw_id", ["999999", "abc", "0", "-1", str(2**63)])
async def test_close_unknown_or_malformed_id_is_404(sessionmaker, raw_id):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/obligations/{raw_id}/done", {}, cookies=cookies)
        assert resp.status == 404


async def test_close_clears_a_pending_checkin_note(sessionmaker):
    async with sessionmaker() as session:
        session.add(
            UserState(
                id=1, chat_id=CHAT_ID, timezone=TIMEZONE, awaiting="checkin_note", awaiting_ref=1
            )
        )
        await session.commit()
    oid = await _open(sessionmaker, "позвонить маме")

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/obligations/{oid}/done", {}, cookies=cookies)
        assert resp.status == 200

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.awaiting is None and state.awaiting_ref is None


async def test_close_never_logs_the_debt_text(sessionmaker, caplog):
    await _seed_state(sessionmaker)
    oid = await _open(sessionmaker, "SECRETDEBT позвонить")

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        with caplog.at_level(logging.DEBUG):
            assert (await _get(client, "/api/obligations", cookies=cookies)).status == 200
            assert (await _post(client, f"/api/obligations/{oid}/done", {}, cookies=cookies)).status == 200

    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "SECRETDEBT" not in blob


async def test_close_rate_limited(sessionmaker):
    await _seed_state(sessionmaker)
    oid = await _open(sessionmaker, "позвонить маме")

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    limiter: WebRateLimiter = app["web_rate_limiter"]
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for _ in range(60):
            assert limiter.check_panel_write() is None
        resp = await _post(client, f"/api/obligations/{oid}/done", {}, cookies=cookies)
        assert resp.status == 429
        assert (await resp.json())["error"] == "rate_limited"

    async with sessionmaker() as session:
        row = await session.get(Obligation, oid)
    assert row.status == "open"
