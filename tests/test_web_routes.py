"""app/web/routes.py end to end (web-chat plan track 2, design sections
4, 6, 7, 10): the HTTP API contract, exercised through a real
aiohttp TestClient against `setup_web`'s routes.

- the full login flow: passphrase -> Telegram code -> session cookie
- GET /api/me reflects each stage
- GET /api/history (empty, then with content, oldest-first)
- POST /api/send: happy path, idempotent client_key retry, blocked
  commands (422), validation (400), unauthenticated (401)
- POST /api/press: happy path, stale/forged data (409)
- POST /api/auth/logout ends the session
- GET /api/events: Last-Event-ID replay and a live event

The Telegram side of login (the code itself) is captured through
conftest's FakeSession, exactly as every other test in this suite reads
what a handler sent -- never by reaching into app/web/auth.py's
CodeStore, which is private implementation this module (deliberately)
never sees.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import uuid

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.core.clock import FrozenClock
from app.web import routes as routes_module
from app.web.hub import WebHub
from app.web.routes import setup_web
from app.web.sink import make_web_bot
from conftest import make_bot
from scripts.web_passphrase import make_hash

CHAT_ID = 4242
PASSPHRASE = "correct horse battery staple"
HASH = make_hash(PASSPHRASE)
ORIGIN = "https://anchor.example.test"
START = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

API_HEADERS = {"Sec-Fetch-Site": "same-origin", "Origin": ORIGIN}
JSON_HEADERS = {**API_HEADERS, "Content-Type": "application/json"}

_CODE_RE = re.compile(r"[0-9A-Z]{4}-[0-9A-Z]{4}")


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


async def _post(client, path, body, cookies: dict | None = None):
    headers = dict(JSON_HEADERS)
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return await client.post(path, headers=headers, data=json.dumps(body))


async def _get(client, path, cookies: dict | None = None, extra_headers: dict | None = None):
    headers = dict(API_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return await client.get(path, headers=headers)


async def _log_in(client, fake_session) -> dict:
    """Passphrase then code; returns {"anchor_s": token}."""
    resp = await _post(client, "/api/auth/passphrase", {"passphrase": PASSPHRASE})
    assert resp.status == 200
    pre_token = _cookie(resp, "__Host-anchor_pre")

    sent_text = fake_session.sent[-1].text
    code = _CODE_RE.search(sent_text).group(0)

    resp = await _post(
        client, "/api/auth/code", {"code": code}, cookies={"__Host-anchor_pre": pre_token}
    )
    assert resp.status == 200
    return {"__Host-anchor_s": _cookie(resp, "__Host-anchor_s")}


# --- login flow ---


async def test_me_before_login(sessionmaker):
    bot, _fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _get(client, "/api/me")
        assert resp.status == 200
        assert await resp.json() == {"authenticated": False, "stage": "none"}


async def test_concurrent_wrong_passphrases_are_serialized_and_capped(sessionmaker, monkeypatch):
    """High-severity finding: check_passphrase_lockout() used to run
    once per request, *before* the awaited scrypt call, so nothing
    counted an in-flight attempt and a burst of concurrent requests
    could all pass the check at once -- an unbounded number of guesses
    per lockout window, plus a burst of real (128 MiB) scrypt calls.

    `web_passphrase_lock` now serializes the whole check-then-act
    sequence: verified two ways here. `max_concurrent` proves at most
    one scrypt call is ever in flight (the memory/CPU DoS fix); the
    401 count proves at most PASSPHRASE_FAIL_LIMIT guesses ever reach
    verify_passphrase at all, however many requests raced to get there
    (the brute-force-limit fix) -- because the lockout is re-checked
    *inside* the lock, a request that queued behind the 5th failure
    sees the lockout that just triggered and short-circuits to 429
    before ever calling verify_passphrase again.
    """
    from app.web.ratelimit import LOCKOUT_ALERT_TEXT, PASSPHRASE_FAIL_LIMIT
    from app.web.routes import _PASSPHRASE_MAX_WAITERS

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)

    concurrent = 0
    max_concurrent = 0
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def spy_verify(passphrase, configured_hash):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        entered.put_nowait(None)
        try:
            await release.wait()  # held open until the test releases it below
            return False  # every guess in this test is "wrong"
        finally:
            concurrent -= 1

    monkeypatch.setattr(routes_module.auth, "verify_passphrase", spy_verify)

    async with TestClient(TestServer(app)) as client:
        burst = [
            asyncio.ensure_future(_post(client, "/api/auth/passphrase", {"passphrase": "wrong"}))
            for _ in range(10)
        ]
        # Wait for the one request that got the lock to actually reach
        # (and block inside) verify_passphrase, then give every other
        # request time to either queue up behind the lock or be turned
        # away by the waiters cap -- all *before* anything is released,
        # so the outcome does not depend on how the event loop happens
        # to interleave the other nine requests' own network I/O.
        await asyncio.wait_for(entered.get(), timeout=2)
        await asyncio.sleep(0.2)

        release.set()  # unblocks the holder; every later holder then
        # sails straight through the already-set event, so the queued
        # ones drain in order without the test managing them one by one.
        responses = await asyncio.gather(*burst)

        statuses = [r.status for r in responses]
        assert max_concurrent == 1  # never more than one scrypt call in flight at once
        # The request that grabs the lock counts against the waiters
        # cap too (the same counter tracks "committed to the lock",
        # holding it or queued for it, not just "queued"), so a single
        # burst this size can never admit more than
        # _PASSPHRASE_MAX_WAITERS real guesses to verify_passphrase at
        # once -- everyone else in the same burst is turned away
        # immediately with 429, never touching verify_passphrase at all.
        admitted = _PASSPHRASE_MAX_WAITERS
        assert statuses.count(401) == admitted
        assert statuses.count(429) == len(burst) - admitted
        assert admitted < PASSPHRASE_FAIL_LIMIT  # this burst alone must not itself cross the lockout threshold
        assert fake.sent == []  # not locked out yet

        # The failure count is still cumulative across separate requests,
        # though: one more wrong guess is the 5th failure overall and
        # triggers the lockout exactly once, alerting over Telegram.
        resp = await _post(client, "/api/auth/passphrase", {"passphrase": "wrong"})
        assert resp.status == 401
        assert len(fake.sent) == 1
        assert fake.sent[0].text == LOCKOUT_ALERT_TEXT

        # A further guess is now rejected by the lockout itself (429)
        # before ever reaching verify_passphrase again -- no repeat alert.
        calls_before = max_concurrent
        resp = await _post(client, "/api/auth/passphrase", {"passphrase": "wrong"})
        assert resp.status == 429
        assert max_concurrent == calls_before  # verify_passphrase was not called again
        assert len(fake.sent) == 1
    assert app["web_passphrase_waiters"]["n"] == 0  # the counter never leaks


async def test_wrong_passphrase_returns_401_generic_error(sessionmaker):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, "/api/auth/passphrase", {"passphrase": "wrong"})
        assert resp.status == 401
        assert (await resp.json()) == {"error": "invalid"}
        assert fake.sent == []  # no code sent on a wrong passphrase


async def test_correct_passphrase_sends_code_only_to_allowed_chat_id(sessionmaker):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, "/api/auth/passphrase", {"passphrase": PASSPHRASE})
        assert resp.status == 200
        assert (await resp.json()) == {"next": "code"}
        pre = resp.cookies["__Host-anchor_pre"]
        assert pre["secure"]
        assert pre["httponly"]
        assert pre["path"] == "/"

    assert len(fake.sent) == 1
    assert fake.sent[0].chat_id == CHAT_ID
    assert _CODE_RE.search(fake.sent[0].text)


async def test_me_reports_code_stage_after_passphrase(sessionmaker):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, "/api/auth/passphrase", {"passphrase": PASSPHRASE})
        pre_token = _cookie(resp, "__Host-anchor_pre")

        resp = await _get(client, "/api/me", cookies={"__Host-anchor_pre": pre_token})
        assert (await resp.json()) == {"authenticated": False, "stage": "code"}


async def test_wrong_code_returns_401(sessionmaker):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, "/api/auth/passphrase", {"passphrase": PASSPHRASE})
        pre_token = _cookie(resp, "__Host-anchor_pre")

        resp = await _post(
            client, "/api/auth/code", {"code": "0000-0000"}, cookies={"__Host-anchor_pre": pre_token}
        )
        assert resp.status == 401
        assert (await resp.json()) == {"error": "invalid"}


async def test_full_login_sets_session_cookie_and_clears_pre_cookie(sessionmaker):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, "/api/auth/passphrase", {"passphrase": PASSPHRASE})
        pre_token = _cookie(resp, "__Host-anchor_pre")
        code = _CODE_RE.search(fake.sent[-1].text).group(0)

        resp = await _post(
            client, "/api/auth/code", {"code": code}, cookies={"__Host-anchor_pre": pre_token}
        )
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True}
        session_cookie = resp.cookies["__Host-anchor_s"]
        assert session_cookie["secure"]
        assert session_cookie["httponly"]
        assert session_cookie.get("samesite") == "Strict"
        pre_cleared = resp.cookies["__Host-anchor_pre"]
        assert pre_cleared.value == "" or int(pre_cleared["max-age"]) <= 0

        resp = await _get(client, "/api/me", cookies={"__Host-anchor_s": session_cookie.value})
        assert (await resp.json()) == {"authenticated": True, "stage": "none"}


async def test_code_accepted_without_dash_case_insensitive(sessionmaker):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _post(client, "/api/auth/passphrase", {"passphrase": PASSPHRASE})
        pre_token = _cookie(resp, "__Host-anchor_pre")
        code = _CODE_RE.search(fake.sent[-1].text).group(0)
        mangled = code.replace("-", "").lower()

        resp = await _post(
            client, "/api/auth/code", {"code": mangled}, cookies={"__Host-anchor_pre": pre_token}
        )
        assert resp.status == 200


async def test_logout_ends_the_session(sessionmaker):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)

        resp = await _post(client, "/api/auth/logout", {}, cookies=cookies)
        assert resp.status == 204

        resp = await _get(client, "/api/history", cookies=cookies)
        assert resp.status == 401


# --- protected endpoints require a session ---


async def test_history_send_press_events_require_auth(sessionmaker):
    bot, _fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _get(client, "/api/history")
        assert resp.status == 401 and (await resp.json())["error"] == "unauthenticated"

        resp = await _post(client, "/api/send", {"text": "hi", "client_key": str(uuid.uuid4())})
        assert resp.status == 401

        resp = await _post(client, "/api/press", {"message_id": -1, "data": "w:resume"})
        assert resp.status == 401

        resp = await _get(client, "/api/events")
        assert resp.status == 401


# --- POST /api/send ---


async def test_send_happy_path_and_idempotent_retry(sessionmaker):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)

        key = str(uuid.uuid4())
        resp = await _post(client, "/api/send", {"text": "привет", "client_key": key}, cookies=cookies)
        assert resp.status == 202
        first_id = (await resp.json())["update_id"]
        assert first_id < 0

        resp = await _post(client, "/api/send", {"text": "привет", "client_key": key}, cookies=cookies)
        assert resp.status == 202
        assert (await resp.json())["update_id"] == first_id  # same key -> same id, no new row


async def test_send_mirrors_the_users_own_message_to_the_hub(sessionmaker):
    """Low-severity finding: the sink publishes only bot output and the
    tail excludes web-origin rows, so the user's own web-typed message
    was never published anywhere -- a second open tab/device saw only
    the bot's reply, with no question above it. POST /api/send now
    mirrors it, under the same (negative) update_id the 202 body
    returns, so app.js can dedupe its own optimistic bubble against the
    echo on this same id."""
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        sub = hub.subscribe()

        resp = await _post(
            client, "/api/send", {"text": "привет", "client_key": str(uuid.uuid4())}, cookies=cookies
        )
        assert resp.status == 202
        update_id = (await resp.json())["update_id"]

        event = await asyncio.wait_for(sub.events().__anext__(), timeout=1)
        assert event.event == "message"
        assert event.data["id"] == update_id
        assert event.data["role"] == "user"
        assert event.data["text"] == "привет"
        sub.close()


async def test_send_rejects_delete_and_export(sessionmaker):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for text in ("/delete", "/export", "/EXPORT@anchorbot"):
            resp = await _post(
                client, "/api/send", {"text": text, "client_key": str(uuid.uuid4())}, cookies=cookies
            )
            assert resp.status == 422
            assert (await resp.json()) == {"error": "blocked"}


@pytest.mark.parametrize(
    "body",
    [
        {"text": "", "client_key": str(uuid.uuid4())},
        {"text": "   ", "client_key": str(uuid.uuid4())},
        {"text": "x" * 4001, "client_key": str(uuid.uuid4())},
        {"text": "hi\x07there", "client_key": str(uuid.uuid4())},  # BEL, a rejected control char
        {"text": "hi \ud800 there", "client_key": str(uuid.uuid4())},  # a lone UTF-16 surrogate
        {"text": "hi", "client_key": "not-a-uuid"},
        {"text": "hi"},  # missing client_key
        {"client_key": str(uuid.uuid4())},  # missing text
    ],
)
async def test_send_validation_returns_400(sessionmaker, body):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/send", body, cookies=cookies)
        assert resp.status == 400
        assert (await resp.json()) == {"error": "bad_request"}


def test_send_allows_newline_and_tab():
    from app.web.routes import _valid_text

    assert _valid_text("line one\nline two\tend") is True
    assert _valid_text("bad\x07byte") is False


def test_send_rejects_a_lone_surrogate():
    """W3 finding: a lone UTF-16 surrogate is not a control character,
    so it passed this check before and failed asyncpg's UTF-8 encoding
    downstream instead -- an unhandled 500 whose traceback logged the
    text as a SQL parameter repr."""
    from app.web.routes import _valid_text

    assert _valid_text("hi \ud800 there") is False
    assert _valid_text("\udfff") is False


# --- POST /api/press ---


async def test_press_happy_path_and_stale_rejection(sessionmaker):
    bot, fake = make_bot()
    app, hub, web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)

        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Остаться", callback_data="w:stay")]]
        )
        message = await web_bot.send_message(CHAT_ID, "Всё в порядке?", reply_markup=markup)

        resp = await _post(
            client, "/api/press", {"message_id": message.message_id, "data": "w:stay"}, cookies=cookies
        )
        assert resp.status == 202

        resp = await _post(
            client,
            "/api/press",
            {"message_id": message.message_id, "data": "w:not-a-real-option"},
            cookies=cookies,
        )
        assert resp.status == 409
        assert (await resp.json()) == {"error": "stale"}


async def test_press_rejects_delete_callback_prefix(sessionmaker):
    bot, fake = make_bot()
    app, hub, web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    hub.register_keyboard(1, [[{"text": "Да", "data": "d:yes:1"}]])  # should never happen in practice
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/press", {"message_id": 1, "data": "d:yes:1"}, cookies=cookies)
        assert resp.status == 409


@pytest.mark.parametrize(
    "body",
    [
        {"message_id": "not-an-int", "data": "w:stay"},
        {"message_id": True, "data": "w:stay"},
        {"message_id": 1, "data": ""},
        {"message_id": 1, "data": "x" * 65},
        {"message_id": 1},
    ],
)
async def test_press_validation_returns_400(sessionmaker, body):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/press", body, cookies=cookies)
        assert resp.status == 400


# --- GET /api/history ---


async def test_history_empty(sessionmaker):
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/history", cookies=cookies)
        assert resp.status == 200
        assert (await resp.json()) == {"messages": [], "has_more": False}


async def test_history_returns_oldest_first_and_paginates(sessionmaker):
    from app.db.models import Message

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with sessionmaker() as session:
        for i in range(3):
            session.add(Message(role="user", content=f"msg {i}", ooc=False, kind="chat"))
        await session.commit()

    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/history?limit=2", cookies=cookies)
        data = await resp.json()
        assert [m["text"] for m in data["messages"]] == ["msg 1", "msg 2"]
        assert data["has_more"] is True

        oldest = data["messages"][0]["id"]
        resp = await _get(client, f"/api/history?before={oldest}&limit=2", cookies=cookies)
        data = await resp.json()
        assert [m["text"] for m in data["messages"]] == ["msg 0"]
        assert data["has_more"] is False


# --- GET /api/events (SSE) ---


async def test_sse_replays_backlog_and_streams_a_live_event(sessionmaker, monkeypatch):
    monkeypatch.setattr(routes_module, "SSE_MAX_LIFETIME_S", 0.6)
    monkeypatch.setattr(routes_module, "SSE_KEEPALIVE_S", 0.05)
    monkeypatch.setattr(routes_module, "SSE_SESSION_RECHECK_S", 60.0)

    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)

        hub.publish_message(
            id=-1, role="assistant", text="из бэклога", kind="chat", keyboard=None, ts=START
        )

        resp = await _get(
            client, "/api/events", cookies=cookies, extra_headers={"Last-Event-ID": "0"}
        )
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")

        async def _publish_soon():
            await asyncio.sleep(0.1)
            hub.publish_toast("подсказка")

        asyncio.ensure_future(_publish_soon())

        body = await asyncio.wait_for(resp.content.read(), timeout=5)

    text = body.decode("utf-8")
    frames = [frame for frame in text.split("\n\n") if frame.strip() and not frame.startswith(":")]
    events = []
    for frame in frames:
        lines = frame.split("\n")
        event_line = next(line for line in lines if line.startswith("event: "))
        data_line = next(line for line in lines if line.startswith("data: "))
        events.append((event_line[len("event: ") :], json.loads(data_line[len("data: ") :])))

    kinds = [kind for kind, _data in events]
    assert "message" in kinds
    message_data = next(data for kind, data in events if kind == "message")
    assert message_data["text"] == "из бэклога"
    assert message_data["id"] == -1

    assert "toast" in kinds
    toast_data = next(data for kind, data in events if kind == "toast")
    assert toast_data["text"] == "подсказка"

    assert ": ka" in text  # the keepalive fired at least once in the interval


# --- GET /static/{path:.+}: the manifest (W1 plan step 2) ---

# These tests point `routes_module.STATIC_DIR` at a throwaway directory
# they build themselves, so the manifest's mechanics (content types,
# 304, every 404 shape) are exercised deterministically -- independent
# of whatever the frontend track has or has not written under the real
# app/web/static/ yet.


def _write_static_tree(base):
    (base / "app").mkdir()
    (base / "vendor").mkdir()
    (base / "app" / "main.js").write_text("console.log('hi');", encoding="utf-8")
    (base / "app.css").write_text("body { color: red }", encoding="utf-8")
    (base / "icon.svg").write_text("<svg></svg>", encoding="utf-8")
    (base / "vendor" / "preact.module.js").write_text("export {};", encoding="utf-8")
    (base / "vendor" / "VENDOR.lock").write_text("{}", encoding="utf-8")
    (base / "index.html").write_text("<html></html>", encoding="utf-8")
    (base / "app" / "big.js").write_bytes(b"x" * (1024 * 1024 + 1))
    return base


def _build_static_app(monkeypatch, sessionmaker, static_dir):
    monkeypatch.setattr(routes_module, "STATIC_DIR", static_dir)
    bot, _fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    return app


async def test_static_serves_js_css_svg_with_correct_content_types(
    sessionmaker, tmp_path, monkeypatch
):
    _write_static_tree(tmp_path)
    app = _build_static_app(monkeypatch, sessionmaker, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/static/app/main.js")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert (await resp.read()) == b"console.log('hi');"

        resp = await client.get("/static/app.css")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/css; charset=utf-8"

        resp = await client.get("/static/icon.svg")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/svg+xml"

        resp = await client.get("/static/vendor/preact.module.js")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/javascript; charset=utf-8"


async def test_static_304_on_matching_if_none_match(sessionmaker, tmp_path, monkeypatch):
    _write_static_tree(tmp_path)
    app = _build_static_app(monkeypatch, sessionmaker, tmp_path)
    async with TestClient(TestServer(app)) as client:
        first = await client.get("/static/app/main.js")
        etag = first.headers["ETag"]
        assert etag

        second = await client.get("/static/app/main.js", headers={"If-None-Match": etag})
        assert second.status == 304
        assert second.headers["ETag"] == etag
        assert (await second.read()) == b""

        third = await client.get(
            "/static/app/main.js", headers={"If-None-Match": '"stale-etag"'}
        )
        assert third.status == 200

        # Low-severity finding: a proxy/CDN that recompresses the body
        # weakens the validator to W/"<sha>" (still the same sha256, per
        # RFC 9110's weak-comparison rule this uses `.value` for, not
        # `==` on the raw header), and a client may send a
        # comma-separated list rather than one value -- either used to
        # miss a 304 entirely because the old check compared the raw
        # header string to the quoted ETag by exact equality.
        weak = await client.get(
            "/static/app/main.js", headers={"If-None-Match": f'W/{etag}'}
        )
        assert weak.status == 304

        listed = await client.get(
            "/static/app/main.js", headers={"If-None-Match": f'"other-etag", {etag}'}
        )
        assert listed.status == 304

        wildcard = await client.get("/static/app/main.js", headers={"If-None-Match": "*"})
        assert wildcard.status == 304


@pytest.mark.parametrize(
    "path",
    [
        "/static/does-not-exist.js",  # missing
        "/static/vendor/VENDOR.lock",  # disallowed extension
        "/static/app",  # a directory, no trailing slash
        "/static/app/",  # a directory, trailing slash
        "/static//etc/passwd",  # an absolute-looking embedded path
        "/static/index.html",  # served only at "/", never under /static/
        "/static/app/big.js",  # over the 1 MiB cap
    ],
)
async def test_static_404_for_traversal_missing_and_disallowed_paths(
    sessionmaker, tmp_path, monkeypatch, path
):
    _write_static_tree(tmp_path)
    app = _build_static_app(monkeypatch, sessionmaker, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path)
        assert resp.status == 404


@pytest.mark.parametrize(
    "raw_path",
    [
        "/static/%2e%2e/app.css",  # percent-encoded ".." segment
        "/static/%2e%2e%2fapp.css",  # percent-encoded ".." + slash
        "/static/app%2f..%2fapp.css",  # fully percent-encoded traversal
    ],
)
async def test_static_404_for_percent_encoded_traversal(
    sessionmaker, tmp_path, monkeypatch, raw_path
):
    """Low-severity finding: these were checked manually against the
    real handler (they do 404, since the decoded string is simply not a
    key `_build_static_manifest` ever populated) but nothing regression-
    tested it. A *literal* "app/../app.css" is not a meaningful case to
    add alongside these: `yarl.URL.join` (what both a real browser's
    fetch() and this test's own client use to build the request line)
    normalizes a dot segment away before any request is ever sent, with
    or without `encoded=True` -- by the time any conforming HTTP client
    could send it, it already reads "app.css", a real file, not a
    traversal attempt. Percent-encoding is what survives that
    normalization and is what `yarl.URL(..., encoded=True)` lets this
    test's request line actually carry unnormalized, exactly as
    aiohttp's own request-line decoding would see it."""
    from yarl import URL

    _write_static_tree(tmp_path)
    app = _build_static_app(monkeypatch, sessionmaker, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(URL(raw_path, encoded=True))
        assert resp.status == 404


async def test_static_404_carries_security_headers(sessionmaker, tmp_path, monkeypatch):
    """Low-severity finding: a raised web.HTTPNotFound() used to reach
    the client with no security headers at all, because the middleware
    only stamped them onto a *returned* response, never one that
    propagated out of `await handler(request)` as an exception -- every
    /static/* miss, including every traversal probe above, was served
    with no nosniff and no CSP."""
    _write_static_tree(tmp_path)
    app = _build_static_app(monkeypatch, sessionmaker, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/static/vendor/VENDOR.lock")
        assert resp.status == 404
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Content-Security-Policy"]


async def test_static_skips_a_symlink(sessionmaker, tmp_path, monkeypatch):
    _write_static_tree(tmp_path)
    target = tmp_path / "app" / "main.js"
    link = tmp_path / "app" / "linked.js"
    link.symlink_to(target)
    app = _build_static_app(monkeypatch, sessionmaker, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/static/app/linked.js")
        assert resp.status == 404


async def test_static_security_headers_present(sessionmaker, tmp_path, monkeypatch):
    _write_static_tree(tmp_path)
    app = _build_static_app(monkeypatch, sessionmaker, tmp_path)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/static/app/main.js")
        assert resp.status == 200
        assert resp.headers["Content-Security-Policy"]
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Cache-Control"] == "no-cache"


async def test_static_security_headers_present_on_the_real_shipped_files(sessionmaker):
    """Same assertion as the isolated test above, but against whatever
    the real app/web/static/ tree actually contains right now -- per
    the task brief, `app/**/*.js` may not exist yet (a concurrent
    track's job), so this picks whichever real `.js` file it finds
    under `static/app/` or `static/vendor/` instead of hardcoding
    `app/main.js`.
    """
    real_static = routes_module.STATIC_DIR
    candidates = sorted((real_static / "app").rglob("*.js")) + sorted(
        (real_static / "vendor").rglob("*.js")
    )
    if not candidates:
        pytest.skip("no real static .js file exists yet under app/web/static/")
    rel = candidates[0].relative_to(real_static).as_posix()

    bot, _fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/static/{rel}")
        assert resp.status == 200
        assert resp.headers["Content-Security-Policy"]
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Cache-Control"] == "no-cache"
