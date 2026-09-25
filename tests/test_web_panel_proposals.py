"""app/web/panels/proposals.py end to end (W2 roadmap section 4): GET
/api/proposals and POST /api/proposals/{id}/accept|reject.

Covers: 401/403/429, GET's pending/recent shape, accept/reject through
app/core/proposal.py (source stays "button" -- see the module
docstring for why that is not a bug), 404 for an unknown id, 409 for a
non-pending one (with the current proposal in the body), that a
Telegram-issued keyboard is retired with the real "✅ Принято"/"✖️
Отклонено" outcome text (via the real bot, never a chat message), and
the invalidate("proposals")/invalidate("state") publishing rule.
"""

from __future__ import annotations

import json
import re

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.core import proposal
from app.core.clock import FrozenClock
from app.db.models import UserState
from app.web.hub import WebHub
from app.web.ratelimit import WebRateLimiter
from app.web.routes import setup_web
from app.web.sink import make_web_bot
from conftest import FakeSession, make_bot
from scripts.web_passphrase import make_hash


class _EditFailsSession(FakeSession):
    """See tests/test_web_panel_state.py's identical helper: a
    FakeSession whose `editMessageText` always raises, like a deleted
    Telegram message or a network error would.
    """

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
        return await super().make_request(bot, method, timeout)


def _edit_failing_bot() -> tuple[Bot, _EditFailsSession]:
    fake = _EditFailsSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake

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


async def _make_proposal(sessionmaker, clock, field: str, value: str, *, with_message: bool = False) -> int:
    async with sessionmaker() as session:
        created, _ = await proposal.create(
            session, clock, field=field, value=value, reason="потому что"
        )
        pid = created.id
    if with_message:
        async with sessionmaker() as session:
            await proposal.set_message_id(session, pid, 900)
    return pid


# --- GET /api/proposals ------------------------------------------------


async def test_proposals_requires_a_session(sessionmaker):
    bot, _fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _get(client, "/api/proposals")
        assert resp.status == 401


async def test_proposals_rejects_a_foreign_origin(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(
            client,
            "/api/proposals",
            cookies=cookies,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert resp.status == 403


async def test_get_proposals_empty(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/proposals", cookies=cookies)
        assert resp.status == 200
        assert (await resp.json()) == {"pending": None, "recent": []}


async def test_get_proposals_returns_pending_with_russian_label(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(sessionmaker, clock, proposal.DUE_ACTION, "сдать отчёт")

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/proposals", cookies=cookies)
        body = await resp.json()

    assert body["pending"]["id"] == pid
    assert body["pending"]["field"] == "due_action"
    assert body["pending"]["field_label"] == "Главное действие"
    assert body["pending"]["value"] == "сдать отчёт"
    assert body["pending"]["status"] == "pending"
    assert body["recent"] == []


async def test_get_proposals_returns_recent_decided_newest_first(sessionmaker, clock):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        first, _ = await proposal.create(
            session, clock, field=proposal.DUE_ACTION, value="первое", reason=None
        )
        first_id = first.id
    async with sessionmaker() as session:
        await proposal.reject(session, clock, first_id)

    async with sessionmaker() as session:
        second, _ = await proposal.create(
            session, clock, field=proposal.FOCUS_ON, value="on", reason=None
        )
        second_id = second.id
    async with sessionmaker() as session:
        await proposal.accept(session, clock, second_id)

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/proposals", cookies=cookies)
        body = await resp.json()

    assert [row["id"] for row in body["recent"]] == [second_id, first_id]
    assert body["recent"][0]["status"] == "accepted"
    assert body["recent"][1]["status"] == "rejected"


# --- POST accept/reject -------------------------------------------------


async def test_accept_due_action_writes_state_with_source_button(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(sessionmaker, clock, proposal.DUE_ACTION, "сдать отчёт до пятницы")

    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        sent_before = len(fake.sent)  # the login code itself was sent above
        resp = await _post(client, f"/api/proposals/{pid}/accept", {}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()

    assert body["proposal"]["status"] == "accepted"

    from sqlalchemy import select
    from app.db.models import StateChange

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
        changes = list((await session.execute(select(StateChange))).scalars())
    assert state.due_action == "сдать отчёт до пятницы"
    assert {(c.field, c.source) for c in changes} == {
        ("due_action", "button"), ("due_set_at", "button")
    }

    # Silent in Telegram: accept/reject never sends a chat message.
    assert len(fake.sent) == sent_before

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "proposals" in topics
    assert "state" in topics  # accept of due_action also invalidates state


async def test_accept_rule_does_not_invalidate_state(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(sessionmaker, clock, proposal.RULE, "не пить кофе вечером")

    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/proposals/{pid}/accept", {}, cookies=cookies)
        assert resp.status == 200

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "proposals" in topics
    assert "state" not in topics


async def test_reject_changes_no_state(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(sessionmaker, clock, proposal.DUE_ACTION, "сдать отчёт")

    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/proposals/{pid}/reject", {}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
    assert body["proposal"]["status"] == "rejected"

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.due_action is None

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "proposals" in topics
    assert "state" not in topics


async def test_accept_retires_the_telegram_keyboard_with_the_outcome_text(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(
        sessionmaker, clock, proposal.DUE_ACTION, "сдать отчёт", with_message=True
    )

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/proposals/{pid}/accept", {}, cookies=cookies)
        assert resp.status == 200

    assert len(fake.edits) == 1
    assert fake.edits[0].message_id == 900
    assert "✅ Принято" in fake.edits[0].text
    assert fake.edits[0].reply_markup is None


async def test_reject_retires_the_telegram_keyboard_with_the_outcome_text(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(
        sessionmaker, clock, proposal.FOCUS_ON, "on", with_message=True
    )

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/proposals/{pid}/reject", {}, cookies=cookies)
        assert resp.status == 200

    assert len(fake.edits) == 1
    assert "✖️ Отклонено" in fake.edits[0].text


async def test_accept_without_a_telegram_message_sends_no_edit(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(sessionmaker, clock, proposal.DUE_ACTION, "сдать отчёт")

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/proposals/{pid}/accept", {}, cookies=cookies)
        assert resp.status == 200
    assert fake.edits == []


# --- 404 / 409 -----------------------------------------------------------


async def test_accept_unknown_id_returns_404(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/proposals/999999/accept", {}, cookies=cookies)
        assert resp.status == 404
        assert (await resp.json()) == {"error": "not_found"}


async def test_accept_non_pending_returns_409_with_the_proposal(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(sessionmaker, clock, proposal.DUE_ACTION, "сдать отчёт")
    async with sessionmaker() as session:
        await proposal.reject(session, clock, pid)

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/proposals/{pid}/accept", {}, cookies=cookies)
        assert resp.status == 409
        body = await resp.json()
    assert body["error"] == "not_pending"
    assert body["proposal"]["id"] == pid
    assert body["proposal"]["status"] == "rejected"


async def test_reject_non_pending_returns_409(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(sessionmaker, clock, proposal.DUE_ACTION, "сдать отчёт")
    async with sessionmaker() as session:
        await proposal.accept(session, clock, pid)

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/proposals/{pid}/reject", {}, cookies=cookies)
        assert resp.status == 409
        body = await resp.json()
    assert body["error"] == "not_pending"
    assert body["proposal"]["status"] == "accepted"


async def test_accept_with_a_non_numeric_id_returns_404(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/proposals/not-a-number/accept", {}, cookies=cookies)
        assert resp.status == 404


# --- 429 -------------------------------------------------------------------


async def test_accept_survives_a_telegram_failure_showing_the_outcome(sessionmaker, clock):
    """accept() has already committed (status=accepted, the due/focus
    user_state write) by the time show_decision_outcome runs -- a
    Telegram-side failure there (message deleted, network error) must
    not turn that already-successful decision into a 500, and must not
    skip either invalidate.
    """
    await _seed_state(sessionmaker)
    pid = await _make_proposal(
        sessionmaker, clock, proposal.DUE_ACTION, "сдать отчёт", with_message=True
    )

    bot, fake = _edit_failing_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/proposals/{pid}/accept", {}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
    assert body["proposal"]["status"] == "accepted"

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.due_action == "сдать отчёт"  # already committed regardless of the Telegram failure

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "proposals" in topics and "state" in topics


async def test_reject_survives_a_telegram_failure_showing_the_outcome(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(
        sessionmaker, clock, proposal.DUE_ACTION, "сдать отчёт", with_message=True
    )

    bot, fake = _edit_failing_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/proposals/{pid}/reject", {}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
    assert body["proposal"]["status"] == "rejected"
    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "proposals" in topics


async def test_proposal_decision_rate_limited(sessionmaker, clock):
    await _seed_state(sessionmaker)
    pid = await _make_proposal(sessionmaker, clock, proposal.DUE_ACTION, "сдать отчёт")

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    limiter: WebRateLimiter = app["web_rate_limiter"]
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for _ in range(60):
            assert limiter.check_panel_write() is None
        resp = await _post(client, f"/api/proposals/{pid}/accept", {}, cookies=cookies)
        assert resp.status == 429
        body = await resp.json()
        assert body["error"] == "rate_limited"
