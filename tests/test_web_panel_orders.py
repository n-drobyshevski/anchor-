"""app/web/panels/orders.py end to end: GET /api/orders, POST
/api/orders (create an active order) and POST /api/orders/{id}/retire.

Covers: 401/403/429, the active list with its Russian cadence label
(proposed and retired orders left out), a create through
app/core/orders.py's create_active with source "user" and the same
cadence tokens `/order` accepts, 422 for a bad cadence, empty text and
the cap (with `/order`'s own reply), a retire and its 404 on a repeat,
invalidate("orders") and ("checkin") -- the check-in form asks about
every active order -- silence in Telegram, and no order text in logs.
"""

from __future__ import annotations

import json
import logging
import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import StandingOrder, UserState
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




def _topics(hub: WebHub) -> list[str]:
    return [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]


async def _app_client(sessionmaker):
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    return app, hub, fake

async def _order(sessionmaker, text: str, status: str = "active", cadence: str = "daily") -> int:
    async with sessionmaker() as session:
        row = StandingOrder(text=text, cadence=cadence, status=status, source="user")
        session.add(row)
        await session.commit()
        return row.id


async def _all_orders(sessionmaker) -> list[StandingOrder]:
    from sqlalchemy import select

    async with sessionmaker() as session:
        return list((await session.execute(select(StandingOrder).order_by(StandingOrder.id))).scalars())


async def test_orders_requires_a_session(sessionmaker):
    app, _hub, _fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        assert (await _get(client, "/api/orders")).status == 401
        assert (await _post(client, "/api/orders", {"text": "x", "cadence": "daily"})).status == 401
        assert (await _post(client, "/api/orders/1/retire", {})).status == 401


async def test_orders_rejects_a_foreign_origin(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(
            client,
            "/api/orders",
            cookies=cookies,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert resp.status == 403


async def test_get_orders_lists_active_only(sessionmaker):
    await _seed_state(sessionmaker)
    active = await _order(sessionmaker, "Прогулка 20 минут")
    await _order(sessionmaker, "Предложенное", status="proposed")
    await _order(sessionmaker, "Снятое", status="retired")
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/orders", cookies=cookies)).json()
    assert [item["id"] for item in body["items"]] == [active]
    assert body["items"][0]["cadence_label"] == "ежедневно"
    assert body["limits"] == {"text_max": 200, "active_max": 5}


@pytest.mark.parametrize(
    ("token", "cadence", "weekday", "label"),
    [
        ("daily", "daily", None, "ежедневно"),
        ("weekdays", "weekdays", None, "по будням"),
        ("weekly:3", "weekly", 3, "по средам"),
        ("once", "once", None, "один раз"),
    ],
)
async def test_create_order_with_each_cadence(sessionmaker, token, cadence, weekday, label):
    await _seed_state(sessionmaker)
    app, hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        sent_before = len(fake.sent)
        resp = await _post(client, "/api/orders", {"text": " Прогулка ", "cadence": token}, cookies=cookies)
        assert resp.status == 201
        body = await (await _get(client, "/api/orders", cookies=cookies)).json()
        assert len(fake.sent) == sent_before

    rows = await _all_orders(sessionmaker)
    assert [(r.text, r.cadence, r.weekday, r.status, r.source) for r in rows] == [
        ("Прогулка", cadence, weekday, "active", "user")
    ]
    assert body["items"][0]["cadence_label"] == label
    assert {"orders", "checkin"} <= set(_topics(hub))


@pytest.mark.parametrize("token", ["weekly", "weekly:8", "monthly", ""])
async def test_create_rejects_a_bad_cadence(sessionmaker, token):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/orders", {"text": "Прогулка", "cadence": token}, cookies=cookies)
        assert resp.status == 422
        assert (await resp.json())["detail"] == "bad_cadence"
    assert await _all_orders(sessionmaker) == []


async def test_create_rejects_empty_text_and_the_cap(sessionmaker):
    await _seed_state(sessionmaker)
    for i in range(5):
        await _order(sessionmaker, f"Дело {i}")
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/orders", {"text": "  ", "cadence": "daily"}, cookies=cookies)
        assert (await resp.json())["detail"] == "empty"
        resp = await _post(client, "/api/orders", {"text": "Ещё одно", "cadence": "daily"}, cookies=cookies)
        assert resp.status == 422
        body = await resp.json()
    assert body["detail"] == "cap"
    assert body["message"] == "Сначала сними одну из договорённостей."


@pytest.mark.parametrize("body", [{"text": 5, "cadence": "daily"}, {"text": "x", "cadence": 1}, {"text": "a\x01b", "cadence": "daily"}])
async def test_create_rejects_a_malformed_body(sessionmaker, body):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        assert (await _post(client, "/api/orders", body, cookies=cookies)).status == 400


async def test_retire_then_404(sessionmaker):
    await _seed_state(sessionmaker)
    oid = await _order(sessionmaker, "Прогулка")
    app, hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        assert (await _post(client, f"/api/orders/{oid}/retire", {}, cookies=cookies)).status == 200
        assert (await _post(client, f"/api/orders/{oid}/retire", {}, cookies=cookies)).status == 404
        assert (await _post(client, "/api/orders/abc/retire", {}, cookies=cookies)).status == 404

    rows = await _all_orders(sessionmaker)
    assert rows[0].status == "retired" and rows[0].retired_at is not None
    assert _topics(hub).count("orders") == 1


async def test_orders_never_log_order_text(sessionmaker, caplog):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        with caplog.at_level(logging.DEBUG):
            await _post(client, "/api/orders", {"text": "SECRETORDER гулять", "cadence": "daily"}, cookies=cookies)
            await _get(client, "/api/orders", cookies=cookies)
    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "SECRETORDER" not in blob


async def test_create_rate_limited(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    limiter: WebRateLimiter = app["web_rate_limiter"]
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for _ in range(60):
            assert limiter.check_panel_write() is None
        resp = await _post(client, "/api/orders", {"text": "Прогулка", "cadence": "daily"}, cookies=cookies)
        assert resp.status == 429
    assert await _all_orders(sessionmaker) == []
