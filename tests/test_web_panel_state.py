"""app/web/panels/state.py end to end (W2 roadmap section 4): GET
/api/state and the five write endpoints, exercised through a real
aiohttp TestClient against setup_web's routes -- the same shape
tests/test_web_routes.py already uses for track 2.

Covers: 401/403/429, each write's 422 validation, that every write goes
through app/core/commands.py with source="web" (an audit row, not a
bare update), the side effects (proposal expiry + Telegram button
retirement, cancel_outbound on a set /quiet, clear_awaiting, the
invalidate SSE events), that the pause toggle enqueues the synthetic
/out or /in update rather than writing user_state directly, and that
StateDTO never carries a forbidden field.
"""

from __future__ import annotations

import datetime
import json

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.config import Settings
from app.core import proposal
from app.core.clock import FrozenClock
from app.core.state import get_state
from app.db.models import Outbound, StateChange, TelegramUpdate, UserState
from app.web.hub import WebHub
from app.web.ratelimit import MAX_PENDING_WEB_ROWS, WebRateLimiter
from app.web.routes import setup_web
from app.web.sink import make_web_bot
from conftest import FakeSession, make_bot
from scripts.web_passphrase import make_hash


class _EditFailsSession(FakeSession):
    """A FakeSession whose `editMessageText` always raises, like a
    deleted Telegram message or a network error would -- for the
    review finding that a Telegram-side failure retiring a stale
    proposal's buttons must not turn an already-committed web write
    into a 500.
    """

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
        return await super().make_request(bot, method, timeout)


def _edit_failing_bot() -> tuple[Bot, _EditFailsSession]:
    fake = _EditFailsSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake

pytestmark = pytest.mark.asyncio

CHAT_ID = 4242
PASSPHRASE = "correct horse battery staple"
HASH = make_hash(PASSPHRASE)
ORIGIN = "https://anchor.example.test"
TIMEZONE = "Europe/Paris"
START = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)

API_HEADERS = {"Sec-Fetch-Site": "same-origin", "Origin": ORIGIN}
JSON_HEADERS = {**API_HEADERS, "Content-Type": "application/json"}
DAY = datetime.date(2026, 1, 1)


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
    sent_text = fake_session.sent[-1].text
    import re

    code = re.search(r"[0-9A-Z]{4}-[0-9A-Z]{4}", sent_text).group(0)
    resp = await _post(
        client, "/api/auth/code", {"code": code}, cookies={"__Host-anchor_pre": pre_token}
    )
    assert resp.status == 200
    return {"__Host-anchor_s": _cookie(resp, "__Host-anchor_s")}


async def _seed(sessionmaker, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()


async def _changes(sessionmaker) -> list[StateChange]:
    async with sessionmaker() as session:
        return list((await session.execute(select(StateChange))).scalars())


# --- GET /api/state --------------------------------------------------------


async def test_state_requires_a_session(sessionmaker):
    bot, _fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _get(client, "/api/state")
        assert resp.status == 401
        assert (await resp.json()) == {"error": "unauthenticated"}


async def test_state_rejects_a_foreign_origin(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(
            client,
            "/api/state",
            cookies=cookies,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert resp.status == 403


async def test_state_shape_has_no_forbidden_fields(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/state", cookies=cookies)
        assert resp.status == 200
        body = await resp.json()

    raw = json.dumps(body)
    for forbidden in ("chat_id", "awaiting", "awaiting_ref"):
        assert forbidden not in raw

    assert body["focus"] == {"on": False, "since": None}
    assert body["due"] == {"action": None, "set_at": None}
    assert body["streak"] == 0
    assert body["timezone"] == TIMEZONE
    assert body["paused"] is False
    assert body["spend"]["cap_usd"] == 1.0
    assert body["spend"]["today_usd"] == 0.0
    assert body["counts"] == {
        "memories": 0, "welfare_today": 0, "distill_today": 0, "search_today": 0
    }
    assert body["limits"]["due_max_len"] > 0


async def test_state_paused_reflects_persona_active(sessionmaker):
    await _seed(sessionmaker, persona_active=False)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/state", cookies=cookies)
        body = await resp.json()
    assert body["paused"] is True


# --- POST /api/state/due ----------------------------------------------------


async def test_post_due_sets_the_action_with_source_web(sessionmaker):
    await _seed(sessionmaker, awaiting="checkin_note", awaiting_ref=1)
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/due", {"text": "сдать отчёт"}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()

    assert body["state"]["due"]["action"] == "сдать отчёт"
    changes = await _changes(sessionmaker)
    assert {(c.field, c.source) for c in changes if c.field.startswith("due")} == {
        ("due_action", "web"), ("due_set_at", "web")
    }
    # Silent in Telegram: no message sent over the real bot.
    assert fake.sent == [] or all("отчёт" not in m.text for m in fake.sent)

    async with sessionmaker() as session:
        state = await get_state(session)
    assert state.awaiting is None and state.awaiting_ref is None  # clear_awaiting ran

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "state" in topics


async def test_post_due_rejects_empty_text(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/due", {"text": "   "}, cookies=cookies)
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "empty"}


async def test_post_due_rejects_too_long_text(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    from app.core.commands import DUE_ACTION_MAX_LEN

    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client, "/api/state/due", {"text": "x" * (DUE_ACTION_MAX_LEN + 1)}, cookies=cookies
        )
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "too_long"}


async def test_post_due_expires_a_pending_proposal_and_retires_its_buttons(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        created, _ = await proposal.create(
            session, clock, field=proposal.DUE_ACTION, value="что-то другое", reason=None
        )
        pid = created.id
        await proposal.set_message_id(session, pid, 900)

    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/due", {"text": "сдать отчёт"}, cookies=cookies)
        assert resp.status == 200

    async with sessionmaker() as session:
        row = await session.get(proposal.Proposal, pid)
    assert row.status == proposal.EXPIRED
    assert any("Устарело" in edit.text for edit in fake.edits)
    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "proposals" in topics and "state" in topics


# --- POST /api/state/focus --------------------------------------------------


async def test_post_focus_sets_on_and_since(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/focus", {"on": True}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
    assert body["state"]["focus"]["on"] is True
    assert body["state"]["focus"]["since"] is not None
    changes = await _changes(sessionmaker)
    assert {(c.field, c.source) for c in changes} == {
        ("focus_on", "web"), ("focus_since", "web")
    }


async def test_post_focus_rejects_a_non_bool(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/focus", {"on": "yes"}, cookies=cookies)
        assert resp.status == 400


# --- POST /api/state/quiet --------------------------------------------------


async def test_post_quiet_sets_until_and_cancels_outbound(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind="morning", local_date=DAY, bucket=0, planned_for=START, status="planned"
            )
        )
        await session.commit()

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    until = (START + datetime.timedelta(hours=2)).isoformat()
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/quiet", {"until": until}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
    assert body["state"]["quiet_until"] is not None

    async with sessionmaker() as session:
        rows = list((await session.execute(select(Outbound))).scalars())
    assert [r.status for r in rows] == ["cancelled"]


async def test_post_quiet_off_clears_and_does_not_cancel(sessionmaker):
    await _seed(sessionmaker, quiet_until=START + datetime.timedelta(hours=5))
    async with sessionmaker() as session:
        session.add(
            Outbound(
                kind="morning", local_date=DAY, bucket=0, planned_for=START, status="planned"
            )
        )
        await session.commit()

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/quiet", {"until": None}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
    assert body["state"]["quiet_until"] is None

    async with sessionmaker() as session:
        rows = list((await session.execute(select(Outbound))).scalars())
    assert [r.status for r in rows] == ["planned"]


async def test_post_quiet_rejects_a_past_timestamp(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    until = (START - datetime.timedelta(hours=1)).isoformat()
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/quiet", {"until": until}, cookies=cookies)
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "past"}


@pytest.mark.parametrize("bad", ["not-a-date", "2026-01-01T12:00:00"])  # no offset
async def test_post_quiet_rejects_a_malformed_timestamp(sessionmaker, bad):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/quiet", {"until": bad}, cookies=cookies)
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "bad_time"}


async def test_post_quiet_clamps_a_far_future_timestamp(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    until = (START + datetime.timedelta(days=30)).isoformat()
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/quiet", {"until": until}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
    quiet_until = datetime.datetime.fromisoformat(body["state"]["quiet_until"])
    assert quiet_until == START + datetime.timedelta(days=7)


# --- POST /api/state/timezone -----------------------------------------------


async def test_post_timezone_sets_the_zone(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client, "/api/state/timezone", {"tz": "Asia/Tokyo"}, cookies=cookies
        )
        assert resp.status == 200
        body = await resp.json()
    assert body["state"]["timezone"] == "Asia/Tokyo"


async def test_post_timezone_rejects_an_unknown_zone(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client, "/api/state/timezone", {"tz": "Europe/Atlantis"}, cookies=cookies
        )
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "unknown_timezone"}
    assert (await _get_state(sessionmaker)).timezone == TIMEZONE


async def _get_state(sessionmaker) -> UserState:
    async with sessionmaker() as session:
        return await get_state(session)


# --- POST /api/state/pause --------------------------------------------------


async def test_post_pause_on_enqueues_out(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/pause", {"on": True}, cookies=cookies)
        assert resp.status == 202
        assert (await resp.json()) == {}

    async with sessionmaker() as session:
        rows = list(
            (
                await session.execute(
                    select(TelegramUpdate).where(TelegramUpdate.update_id < 0)
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].payload["message"]["text"] == "/out"


async def test_post_pause_off_enqueues_in(sessionmaker):
    await _seed(sessionmaker, persona_active=False)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/pause", {"on": False}, cookies=cookies)
        assert resp.status == 202

    async with sessionmaker() as session:
        rows = list(
            (
                await session.execute(
                    select(TelegramUpdate).where(TelegramUpdate.update_id < 0)
                )
            ).scalars()
        )
    assert rows[0].payload["message"]["text"] == "/in"

    # And no change happened synchronously -- pause is queued, not applied
    # in the request itself; the worker (not exercised here) is what
    # eventually flips persona_active.
    assert (await _get_state(sessionmaker)).persona_active is False


# --- 429 -------------------------------------------------------------------


async def test_panel_write_rate_limited(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    limiter: WebRateLimiter = app["web_rate_limiter"]
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for _ in range(60):
            assert limiter.check_panel_write() is None
        resp = await _post(client, "/api/state/focus", {"on": True}, cookies=cookies)
        assert resp.status == 429
        body = await resp.json()
        assert body["error"] == "rate_limited"
        assert "retry_after" in body
        assert resp.headers.get("Retry-After") is not None


# --- review findings --------------------------------------------------------


async def test_post_due_survives_a_telegram_failure_retiring_expired_buttons(sessionmaker, clock):
    """A Telegram-side failure retiring the expired proposal's buttons
    (message deleted, network error) must not turn the already-committed
    due write + proposal expiry into a 500, and must not skip either
    invalidate.
    """
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        created, _ = await proposal.create(
            session, clock, field=proposal.DUE_ACTION, value="что-то другое", reason=None
        )
        pid = created.id
        await proposal.set_message_id(session, pid, 900)

    bot, fake = _edit_failing_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/due", {"text": "сдать отчёт"}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
    assert body["state"]["due"]["action"] == "сдать отчёт"

    async with sessionmaker() as session:
        row = await session.get(proposal.Proposal, pid)
    # Committed regardless of the Telegram failure that came after it.
    assert row.status == proposal.EXPIRED

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "proposals" in topics and "state" in topics


async def test_post_focus_survives_a_telegram_failure_retiring_expired_buttons(sessionmaker, clock):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        created, _ = await proposal.create(
            session, clock, field=proposal.FOCUS_ON, value="on", reason=None
        )
        pid = created.id
        await proposal.set_message_id(session, pid, 900)

    bot, fake = _edit_failing_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/focus", {"on": False}, cookies=cookies)
        assert resp.status == 200

    async with sessionmaker() as session:
        row = await session.get(proposal.Proposal, pid)
    assert row.status == proposal.EXPIRED
    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "proposals" in topics and "state" in topics


@pytest.mark.parametrize(
    "bad",
    [
        "9999-12-31T23:59:59-14:00",  # near datetime.max, overflows on astimezone(utc)
        "0001-01-01T00:00:00+14:00",  # near datetime.min
    ],
)
async def test_post_quiet_rejects_a_timestamp_that_overflows_on_utc_conversion(sessionmaker, bad):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/quiet", {"until": bad}, cookies=cookies)
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "bad_time"}


async def test_post_pause_respects_the_send_rate_limit(sessionmaker):
    await _seed(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    limiter: WebRateLimiter = app["web_rate_limiter"]
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for _ in range(12):
            assert limiter.check_send() is None
        resp = await _post(client, "/api/state/pause", {"on": True}, cookies=cookies)
        assert resp.status == 429
        body = await resp.json()
        assert body["error"] == "rate_limited"


async def test_post_pause_respects_the_pending_web_backlog_cap(sessionmaker):
    await _seed(sessionmaker)
    from sqlalchemy import insert

    async with sessionmaker() as session:
        await session.execute(
            insert(TelegramUpdate),
            [
                {"update_id": -(i + 1), "payload": {}, "status": "pending"}
                for i in range(MAX_PENDING_WEB_ROWS)
            ],
        )
        await session.commit()

    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/pause", {"on": True}, cookies=cookies)
        assert resp.status == 429
        body = await resp.json()
        assert body["error"] == "rate_limited"


async def test_post_pause_is_a_noop_when_already_in_the_requested_state(sessionmaker):
    """`on=True` (pause) while `persona_active` is already False must not
    enqueue a second synthetic /out -- avoids the loop the review
    finding named (a stuck frontend or a script hammering the toggle).
    """
    await _seed(sessionmaker, persona_active=False)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/state/pause", {"on": True}, cookies=cookies)
        assert resp.status == 202
        assert (await resp.json()) == {}

    async with sessionmaker() as session:
        rows = list(
            (
                await session.execute(
                    select(TelegramUpdate).where(TelegramUpdate.update_id < 0)
                )
            ).scalars()
        )
    assert rows == []
