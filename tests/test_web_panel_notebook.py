"""app/web/panels/notebook.py end to end: GET /api/notebook, POST
/api/notebook (add an intention) and POST /api/notebook/{id}/close.

Covers: 401/403/429, the grouped list, an add through
app/core/notebook.py's add_user_intention (source "user") and each of
its refusals as a 422 carrying `/mind add`'s own Russian reply, a close
of an entry of any source (Echo's included), 404 for an unknown,
malformed or already-closed id, invalidate("notebook"), silence in
Telegram, and that entry text never reaches a log line.
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
from app.db.models import NotebookEntry, UserState
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

async def _entry(sessionmaker, kind: str, text: str, source: str) -> int:
    async with sessionmaker() as session:
        row = NotebookEntry(kind=kind, text=text, source=source)
        session.add(row)
        await session.commit()
        return row.id


async def test_notebook_requires_a_session(sessionmaker):
    app, _hub, _fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        assert (await _get(client, "/api/notebook")).status == 401
        assert (await _post(client, "/api/notebook", {"text": "x"})).status == 401
        assert (await _post(client, "/api/notebook/1/close", {})).status == 401


async def test_notebook_rejects_a_foreign_origin(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(
            client,
            "/api/notebook",
            cookies=cookies,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert resp.status == 403


async def test_get_notebook_groups_active_entries(sessionmaker):
    await _seed_state(sessionmaker)
    intention = await _entry(sessionmaker, "intention", "ложиться до полуночи", "user")
    observation = await _entry(sessionmaker, "observation", "устаёт к пятнице", "anchor")
    thread = await _entry(sessionmaker, "open_thread", "разговор с братом", "anchor")
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        body = await (await _get(client, "/api/notebook", cookies=cookies)).json()

    assert body["intentions"] == [{"id": intention, "text": "ложиться до полуночи", "source": "user"}]
    assert [e["id"] for e in body["observations"]] == [observation]
    assert [e["id"] for e in body["threads"]] == [thread]
    assert body["limits"]["text_max"] == 240


async def test_add_intention_is_silent_and_invalidates(sessionmaker):
    await _seed_state(sessionmaker)
    app, hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        sent_before = len(fake.sent)
        resp = await _post(client, "/api/notebook", {"text": "  ложиться до полуночи "}, cookies=cookies)
        assert resp.status == 201
        assert len(fake.sent) == sent_before

    async with sessionmaker() as session:
        from sqlalchemy import select

        rows = list((await session.execute(select(NotebookEntry))).scalars())
    assert [(r.kind, r.text, r.source) for r in rows] == [("intention", "ложиться до полуночи", "user")]
    assert "notebook" in _topics(hub)


@pytest.mark.parametrize(
    ("text", "detail", "message"),
    [
        ("   ", "empty", None),
        ("а" * 241, "too_long", "Слишком длинно — до 240 символов."),
    ],
)
async def test_add_rejects_bad_text(sessionmaker, text, detail, message):
    await _seed_state(sessionmaker)
    app, hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/notebook", {"text": text}, cookies=cookies)
        assert resp.status == 422
        body = await resp.json()
    assert body["detail"] == detail
    if message:
        assert body["message"] == message
    assert "notebook" not in _topics(hub)


async def test_add_rejects_a_duplicate_and_the_cap(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        assert (await _post(client, "/api/notebook", {"text": "ложиться до полуночи"}, cookies=cookies)).status == 201
        resp = await _post(client, "/api/notebook", {"text": "ложиться до полуночи"}, cookies=cookies)
        assert resp.status == 422
        assert (await resp.json())["detail"] == "duplicate"

        for text in ("гулять после обеда", "читать перед сном", "звонить родителям по воскресеньям"):
            assert (await _post(client, "/api/notebook", {"text": text}, cookies=cookies)).status == 201
        resp = await _post(client, "/api/notebook", {"text": "учить испанский"}, cookies=cookies)
        assert resp.status == 422
        body = await resp.json()
    assert body["detail"] == "cap"
    assert body["message"] == "Сначала закрой одно из намерений."


@pytest.mark.parametrize("text", [None, 5, "с\x00нулём", "текст \ud800"])
async def test_add_rejects_a_malformed_body(sessionmaker, text):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/notebook", {"text": text}, cookies=cookies)
        assert resp.status == 400


async def test_close_any_entry_including_echos_own(sessionmaker):
    await _seed_state(sessionmaker)
    observation = await _entry(sessionmaker, "observation", "устаёт к пятнице", "anchor")
    app, hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        sent_before = len(fake.sent)
        resp = await _post(client, f"/api/notebook/{observation}/close", {}, cookies=cookies)
        assert resp.status == 200
        again = await _post(client, f"/api/notebook/{observation}/close", {}, cookies=cookies)
        assert again.status == 404
        assert len(fake.sent) == sent_before

    async with sessionmaker() as session:
        row = await session.get(NotebookEntry, observation)
    assert row.active is False
    assert row.closed_by == "user"
    assert _topics(hub).count("notebook") == 1


@pytest.mark.parametrize("raw_id", ["999999", "abc", "0", str(2**63)])
async def test_close_unknown_or_malformed_id_is_404(sessionmaker, raw_id):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        assert (await _post(client, f"/api/notebook/{raw_id}/close", {}, cookies=cookies)).status == 404


async def test_notebook_never_logs_entry_text(sessionmaker, caplog):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        with caplog.at_level(logging.DEBUG):
            await _post(client, "/api/notebook", {"text": "SECRETNOTE ложиться рано"}, cookies=cookies)
            await _post(client, "/api/notebook", {"text": "SECRETNOTE ложиться рано"}, cookies=cookies)
            await _get(client, "/api/notebook", cookies=cookies)
    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "SECRETNOTE" not in blob


async def test_add_rate_limited(sessionmaker):
    await _seed_state(sessionmaker)
    app, _hub, fake = await _app_client(sessionmaker)
    limiter: WebRateLimiter = app["web_rate_limiter"]
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for _ in range(60):
            assert limiter.check_panel_write() is None
        resp = await _post(client, "/api/notebook", {"text": "ложиться рано"}, cookies=cookies)
        assert resp.status == 429
