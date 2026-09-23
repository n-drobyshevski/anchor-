"""app/web/panels/checkin.py end to end (W4 roadmap section 4): GET/POST
/api/checkin, GET /api/checkins and GET /api/journal through a real
aiohttp TestClient, plus the queued synthetic update fed through the
real dispatcher/worker path (tests/test_web_worker.py's shape).

Covers: 401/403/429, every validation branch, 409 in_progress, the
same-day redo, order answers, the note stored with the row and exactly
one queued `c:n:web` completion carrying the row's own minted message
id (the global note step never opened, so an older queued message can
never be taken for the note), clear_awaiting, retiring a
live Telegram keyboard through the real bot with no SendMessage to
Telegram, the invalidate event, the DTO allow-list, logs without note
text, the worker completing the check-in with exactly one LLM call and
the reply on the hub, a pause-word note not finishing it, and parity
with the Telegram flow for the same answers.
"""

from __future__ import annotations

import datetime
import json
import logging
import re

import pytest
from aiogram import Dispatcher
from aiogram.types import Update
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import insert, select

from app.config import Settings
from app.core import checkin as checkin_core
from app.core import orders as orders_core
from app.core.clock import FrozenClock, SystemClock
from app.core.clock import local_date as clock_local_date
from app.db.models import (
    Checkin,
    CheckinOrderResult,
    Journal,
    Message,
    StandingOrder,
    TelegramUpdate,
    UserState,
)
from app.tg import checkin as checkin_ui
from app.tg.router import build_router
from app.web.hub import WebHub
from app.web.ratelimit import MAX_PENDING_WEB_ROWS, WebRateLimiter
from app.web.routes import setup_web
from app.web.sink import make_web_bot
from app.worker import process_one_update
from conftest import FakeLLMProvider, make_bot
from scripts.web_passphrase import make_hash

pytestmark = pytest.mark.asyncio

CHAT_ID = 4242
PASSPHRASE = "correct horse battery staple"
HASH = make_hash(PASSPHRASE)
ORIGIN = "https://anchor.example.test"
TIMEZONE = "Europe/Paris"
START = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
DAY = datetime.date(2026, 1, 1)

API_HEADERS = {"Sec-Fetch-Site": "same-origin", "Origin": ORIGIN}
JSON_HEADERS = {**API_HEADERS, "Content-Type": "application/json"}

FORBIDDEN_KEYS = ("chat_id", "awaiting", "awaiting_ref", "tg_message_id", "checkin_id")


def _settings(**overrides) -> Settings:
    base = dict(
        ALLOWED_CHAT_ID=CHAT_ID,
        PUBLIC_URL=ORIGIN,
        WEB_PASSPHRASE_HASH=HASH,
        WEB_SESSION_IDLE_HOURS=72,
        WEB_SESSION_MAX_DAYS=14,
        WEB_LOGIN_CODE_TTL_S=300,
        DAILY_USD_CAP=10.0,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, sessionmaker, clock, bot):
    app = web.Application()
    app["settings"] = settings
    app["sessionmaker"] = sessionmaker
    app["clock"] = clock
    app["bot"] = bot
    hub = WebHub()
    web_bot = make_web_bot("123456:TEST", hub)
    setup_web(app, hub=hub, web_bot=web_bot)
    return app, hub, web_bot


async def _post(client, path, body, cookies: dict | None = None, headers=None, raw=None):
    hdrs = dict(headers or JSON_HEADERS)
    if cookies:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return await client.post(path, headers=hdrs, data=raw if raw is not None else json.dumps(body))


async def _get(client, path, cookies: dict | None = None, headers=None):
    hdrs = dict(headers or API_HEADERS)
    if cookies:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return await client.get(path, headers=hdrs)


async def _log_in(client, fake_session) -> dict:
    resp = await _post(client, "/api/auth/passphrase", {"passphrase": PASSPHRASE})
    assert resp.status == 200
    pre_token = resp.cookies["__Host-anchor_pre"].value
    code = re.search(r"[0-9A-Z]{4}-[0-9A-Z]{4}", fake_session.sent[-1].text).group(0)
    resp = await _post(
        client, "/api/auth/code", {"code": code}, cookies={"__Host-anchor_pre": pre_token}
    )
    assert resp.status == 200
    return {"__Host-anchor_s": resp.cookies["__Host-anchor_s"].value}


async def _seed(sessionmaker, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()


async def _add_order(sessionmaker, text: str, cadence: str = "daily") -> int:
    async with sessionmaker() as session:
        row = StandingOrder(text=text, cadence=cadence, status=orders_core.ACTIVE, source="user")
        session.add(row)
        await session.commit()
        return row.id


async def _state(sessionmaker) -> UserState:
    async with sessionmaker() as session:
        return await session.get(UserState, 1, populate_existing=True)


async def _checkins(sessionmaker) -> list[Checkin]:
    async with sessionmaker() as session:
        return list((await session.execute(select(Checkin).order_by(Checkin.id))).scalars())


async def _web_rows(sessionmaker) -> list[TelegramUpdate]:
    async with sessionmaker() as session:
        return list(
            (
                await session.execute(
                    select(TelegramUpdate)
                    .where(TelegramUpdate.update_id < 0)
                    .order_by(TelegramUpdate.created_at)
                )
            ).scalars()
        )


def _events(hub: WebHub):
    sub = hub.subscribe(last_event_id=0)
    sub.close()
    return sub.backlog


def _invalidates(hub: WebHub) -> list[str]:
    return [e.data["topic"] for e in _events(hub) if e.event == "invalidate"]


def _body(**overrides) -> dict:
    body = {"rating": 4, "due_result": None, "orders": [], "note": None}
    body.update(overrides)
    return body


class _Harness:
    """One app + one logged-in client, shared by most tests below."""

    def __init__(self, sessionmaker, clock=None, settings=None):
        self.sessionmaker = sessionmaker
        self.clock = clock or FrozenClock(START)
        self.settings = settings or _settings()
        self.bot, self.fake = make_bot()
        self.app, self.hub, self.web_bot = _build_app(
            self.settings, sessionmaker, self.clock, self.bot
        )
        self.client: TestClient | None = None
        self.cookies: dict = {}

    async def __aenter__(self):
        self.client = TestClient(TestServer(self.app))
        await self.client.__aenter__()
        self.cookies = await _log_in(self.client, self.fake)
        self.sent_after_login = len(self.fake.sent)
        return self

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)

    async def post(self, body, **kw):
        return await _post(self.client, "/api/checkin", body, cookies=self.cookies, **kw)

    async def get(self, path):
        return await _get(self.client, path, cookies=self.cookies)


# --- auth / CSRF --------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [("get", "/api/checkin"), ("post", "/api/checkin"), ("get", "/api/checkins"), ("get", "/api/journal")],
)
async def test_every_route_requires_a_session(sessionmaker, method, path):
    await _seed(sessionmaker)
    bot, _fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        if method == "get":
            resp = await _get(client, path)
        else:
            resp = await _post(client, path, _body())
        assert resp.status == 401
        assert (await resp.json()) == {"error": "unauthenticated"}
    assert await _checkins(sessionmaker) == []


async def test_post_rejects_a_foreign_origin(sessionmaker):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        resp = await h.post(
            _body(), headers={**JSON_HEADERS, "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"}
        )
        assert resp.status == 403
    assert await _checkins(sessionmaker) == []
    assert await _web_rows(sessionmaker) == []


# --- 429 ---------------------------------------------------------------------


async def test_post_respects_the_panel_write_limit(sessionmaker):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        limiter: WebRateLimiter = h.app["web_rate_limiter"]
        for _ in range(60):
            assert limiter.check_panel_write() is None
        resp = await h.post(_body())
        assert resp.status == 429
        body = await resp.json()
        assert body["error"] == "rate_limited"
        assert body["retry_after"] >= 1
        assert resp.headers.get("Retry-After") is not None
    assert await _checkins(sessionmaker) == []


async def test_post_respects_the_send_limit(sessionmaker):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        limiter: WebRateLimiter = h.app["web_rate_limiter"]
        for _ in range(12):
            assert limiter.check_send() is None
        resp = await h.post(_body())
        assert resp.status == 429
        assert (await resp.json())["error"] == "rate_limited"
    assert await _checkins(sessionmaker) == []


async def test_post_respects_the_pending_web_backlog_cap(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(
            insert(TelegramUpdate),
            [
                {"update_id": -(i + 1), "payload": {}, "status": "pending"}
                for i in range(MAX_PENDING_WEB_ROWS)
            ],
        )
        await session.commit()
    async with _Harness(sessionmaker) as h:
        resp = await h.post(_body())
        assert resp.status == 429
        assert (await resp.json())["error"] == "rate_limited"
    assert await _checkins(sessionmaker) == []


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"due_result": None},  # rating missing
        _body(rating=True),  # bool is not an int
        _body(rating=False),
        _body(rating="4"),
        _body(rating=4.0),
        _body(rating=None),
        _body(due_result="none"),  # not a web-selectable value
        _body(due_result="maybe"),
        _body(due_result=1),
        _body(orders=None),
        _body(orders={"id": 1, "result": "done"}),
        _body(orders=["x"]),
        _body(orders=[{"id": 1}]),
        _body(orders=[{"id": 1, "result": "done", "extra": 1}]),
        _body(orders=[{"id": True, "result": "done"}]),
        _body(orders=[{"id": "1", "result": "done"}]),
        _body(orders=[{"id": 2**63, "result": "done"}]),
        _body(orders=[{"id": -(2**63) - 1, "result": "done"}]),
        _body(orders=[{"id": 1, "result": "partial"}]),
        _body(orders=[{"id": 1, "result": None}]),
        _body(orders=[{"id": i, "result": "done"} for i in range(1, 22)]),
        _body(note=5),
        _body(note=["a"]),
        _body(note="строка\x00"),
        _body(note="звонок\x07"),
        _body(note="del\x7f"),
        _body(note="половина \ud800 суррогата"),
    ],
)
async def test_shape_violations_are_400(sessionmaker, body):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        resp = await h.post(body)
        assert resp.status == 400
        assert (await resp.json()) == {"error": "bad_request"}
    assert await _checkins(sessionmaker) == []
    assert await _web_rows(sessionmaker) == []


async def test_non_object_and_malformed_bodies_are_400(sessionmaker):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        resp = await h.post(None, raw="[1, 2]")
        assert resp.status == 400
        resp = await h.post(None, raw="{not json")
        assert resp.status == 400
    assert await _checkins(sessionmaker) == []


async def test_an_oversized_body_is_413(sessionmaker):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        resp = await h.post(_body(note="я" * 20000))
        assert resp.status == 413


@pytest.mark.parametrize(
    "body,detail",
    [
        (_body(rating=0), "rating"),
        (_body(rating=6), "rating"),
        (_body(rating=-1), "rating"),
        (_body(rating=2**70), "rating"),
        (_body(note="я" * 501), "note_too_long"),
        (_body(note="/checkin"), "note_command"),
        (_body(note="   /export всё"), "note_command"),
    ],
)
async def test_content_violations_are_422(sessionmaker, body, detail):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        resp = await h.post(body)
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": detail}
    assert await _checkins(sessionmaker) == []
    assert await _web_rows(sessionmaker) == []


async def test_a_500_char_note_after_strip_is_accepted(sessionmaker):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        resp = await h.post(_body(note="  " + "я" * 500 + "  "))
        assert resp.status == 202
    (row,) = await _checkins(sessionmaker)
    assert row.note == "я" * 500


async def test_due_result_is_required_when_there_is_a_due_action(sessionmaker):
    await _seed(sessionmaker, due_action="сдать отчёт")
    async with _Harness(sessionmaker) as h:
        resp = await h.post(_body(due_result=None))
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "due_result"}
    assert await _checkins(sessionmaker) == []
    assert await _web_rows(sessionmaker) == []


async def test_due_result_is_recorded_as_none_without_a_due_action(sessionmaker):
    """Even if the (stale) form sent an answer: there was no question."""
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        resp = await h.post(_body(due_result="done"))
        assert resp.status == 202
    (row,) = await _checkins(sessionmaker)
    assert row.due_result == checkin_core.NONE


@pytest.mark.parametrize("kind", ["missing", "extra", "duplicate", "unknown"])
async def test_the_order_set_must_match_todays_form(sessionmaker, kind):
    await _seed(sessionmaker)
    first = await _add_order(sessionmaker, "пить воду")
    second = await _add_order(sessionmaker, "гулять")
    submitted = {
        "missing": [{"id": first, "result": "done"}],
        "extra": [
            {"id": first, "result": "done"},
            {"id": second, "result": "no"},
            {"id": second + 100, "result": "no"},
        ],
        "duplicate": [
            {"id": first, "result": "done"},
            {"id": second, "result": "no"},
            {"id": second, "result": "no"},
        ],
        "unknown": [{"id": first, "result": "done"}, {"id": second + 100, "result": "no"}],
    }[kind]
    async with _Harness(sessionmaker) as h:
        resp = await h.post(_body(orders=submitted))
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "orders"}
    assert await _checkins(sessionmaker) == []
    assert await _web_rows(sessionmaker) == []


# --- the happy path -------------------------------------------------------------


async def test_a_note_submit_fills_the_row_and_queues_one_completion(sessionmaker):
    await _seed(sessionmaker, due_action="сдать отчёт")
    order_id = await _add_order(sessionmaker, "пить воду")
    async with _Harness(sessionmaker) as h:
        resp = await h.post(
            _body(
                rating=4,
                due_result="partial",
                orders=[{"id": order_id, "result": "done"}],
                note="  устал, но сделал  ",
            )
        )
        assert resp.status == 202
        assert (await resp.json()) == {}
        assert "checkin" in _invalidates(h.hub)
        assert len(h.fake.sent) == h.sent_after_login, "nothing is sent to Telegram"

    (row,) = await _checkins(sessionmaker)
    assert row.local_date == DAY
    assert row.day_rating == 4
    assert row.due_result == "partial"
    assert row.note == "устал, но сделал"
    assert row.tg_message_id is not None and row.tg_message_id < 0

    async with sessionmaker() as session:
        results = list((await session.execute(select(CheckinOrderResult))).scalars())
    assert [(r.checkin_id, r.order_id, r.result) for r in results] == [(row.id, order_id, "done")]

    state = await _state(sessionmaker)
    assert state.awaiting is None, "the global note step is never opened by the web"
    assert state.streak == 0, "not finished yet"

    rows = await _web_rows(sessionmaker)
    assert len(rows) == 1
    update = Update.model_validate(rows[0].payload)
    assert update.message is None
    assert update.callback_query.data == checkin_ui.WEB_SUBMIT_CALLBACK == "c:n:web"
    assert update.callback_query.message.message_id == row.tg_message_id
    assert update.callback_query.message.chat.id == CHAT_ID


async def test_a_submit_without_a_note_queues_the_completion_with_the_minted_id(sessionmaker):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        resp = await h.post(_body(rating=2, note="   "))
        assert resp.status == 202

    (row,) = await _checkins(sessionmaker)
    rows = await _web_rows(sessionmaker)
    assert len(rows) == 1
    update = Update.model_validate(rows[0].payload)
    assert update.message is None
    assert update.callback_query.data == checkin_ui.WEB_SUBMIT_CALLBACK
    assert row.note is None
    assert update.callback_query.message.message_id == row.tg_message_id
    assert update.update_id != row.tg_message_id
    assert update.callback_query.from_user.id == CHAT_ID


async def test_in_progress_blocks_a_second_submit_with_409(sessionmaker):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        assert (await h.post(_body(note="первая"))).status == 202
        resp = await h.get("/api/checkin")
        assert (await resp.json())["in_progress"] is True
        resp = await h.post(_body(rating=1, note="вторая"))
        assert resp.status == 409
        assert (await resp.json()) == {"error": "in_progress"}
    assert len(await _web_rows(sessionmaker)) == 1
    (row,) = await _checkins(sessionmaker)
    assert row.day_rating == 4


async def test_a_telegram_note_step_plus_an_unrelated_web_row_is_not_in_progress(sessionmaker):
    """A Telegram /checkin at its note step (positive message id) is not
    "a web submission waiting for the worker", even if some unrelated
    web chat message is queued -- the web form must still be usable."""
    await _seed(sessionmaker)
    clock = FrozenClock(START)
    async with sessionmaker() as session:
        row = await checkin_core.start(session, clock, TIMEZONE)
        await checkin_core.set_message_id(session, row.id, 77)
        await checkin_core.set_awaiting_note(session, row.id)
        await session.execute(
            insert(TelegramUpdate).values(update_id=-5, payload={}, status="pending")
        )
        await session.commit()
    async with _Harness(sessionmaker, clock=clock) as h:
        resp = await h.get("/api/checkin")
        assert (await resp.json())["in_progress"] is False
        assert (await h.post(_body())).status == 202


async def test_a_same_day_redo_overwrites_the_row(sessionmaker):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        assert (await h.post(_body(rating=2, note="первая"))).status == 202
        # The worker has run in between (the queued row is done).
        async with sessionmaker() as session:
            for queued in (await session.execute(select(TelegramUpdate))).scalars():
                queued.status = "done"
            await session.commit()
        async with sessionmaker() as session:
            first_id = (await checkin_core.today(session, h.clock, TIMEZONE)).tg_message_id
        assert (await h.post(_body(rating=5))).status == 202

    (row,) = await _checkins(sessionmaker)
    assert row.day_rating == 5
    assert row.note is None
    assert row.tg_message_id != first_id


async def test_submit_clears_a_pending_awaiting_step_first(sessionmaker):
    """As Telegram's command middleware would for /checkin: a leftover
    awaiting step (here, a standing-order counter) is cleared, and no
    note step is opened in its place -- the audit trail shows the clear."""
    await _seed(sessionmaker, awaiting=orders_core.AWAITING_SO_COUNTER, awaiting_ref=99)
    async with _Harness(sessionmaker) as h:
        assert (await h.post(_body())).status == 202
    from app.db.models import StateChange

    async with sessionmaker() as session:
        changes = list(
            (await session.execute(select(StateChange).order_by(StateChange.id))).scalars()
        )
    awaiting_writes = [(c.old_value, c.new_value) for c in changes if c.field == "awaiting"]
    assert awaiting_writes == [(orders_core.AWAITING_SO_COUNTER, None)]
    state = await _state(sessionmaker)
    assert state.awaiting is None


async def test_a_live_telegram_keyboard_is_retired_through_the_real_bot(sessionmaker):
    """A /checkin started in Telegram and then filled in on the web: the
    Telegram message loses its buttons (an edit through the real bot),
    and no new Telegram message is sent."""
    await _seed(sessionmaker)
    clock = FrozenClock(START)
    async with sessionmaker() as session:
        row = await checkin_core.start(session, clock, TIMEZONE)
        await checkin_core.set_message_id(session, row.id, 321)
    async with _Harness(sessionmaker, clock=clock) as h:
        edits_before = len(h.fake.edits)
        assert (await h.post(_body(note="из веба"))).status == 202
        new_edits = h.fake.edits[edits_before:]
        assert len(new_edits) == 1
        assert new_edits[0].message_id == 321
        assert new_edits[0].chat_id == CHAT_ID
        assert new_edits[0].text == checkin_ui.WEB_TAKEOVER_TEXT
        assert new_edits[0].reply_markup is None
        assert len(h.fake.sent) == h.sent_after_login
        # Only the (web) edit/hub events, never a Telegram edit of a web id.
        assert all(e.message_id > 0 for e in h.fake.edits)


async def test_a_web_chat_checkin_keyboard_is_retired_through_the_web_bot(sessionmaker):
    """A /checkin typed into the web chat carries a negative (sink) id:
    it is retired through the web bot, so the chat's keyboard drops."""
    await _seed(sessionmaker)
    clock = FrozenClock(START)
    async with sessionmaker() as session:
        row = await checkin_core.start(session, clock, TIMEZONE)
        await checkin_core.set_message_id(session, row.id, -1234)
    async with _Harness(sessionmaker, clock=clock) as h:
        h.hub.register_keyboard(-1234, [[{"text": "1", "data": "c:r:1"}]])
        edits_before = len(h.fake.edits)
        assert (await h.post(_body())).status == 202
        assert len(h.fake.edits) == edits_before, "the real bot never sees a web id"
        edits = [e for e in _events(h.hub) if e.event == "edit" and e.data["id"] == -1234]
        assert edits and edits[-1].data["keyboard"] is None
        assert h.hub.allow_press(-1234, "c:r:1") is False


async def test_a_telegram_failure_retiring_the_keyboard_does_not_fail_the_submit(sessionmaker):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    await _seed(sessionmaker)
    clock = FrozenClock(START)
    async with sessionmaker() as session:
        row = await checkin_core.start(session, clock, TIMEZONE)
        await checkin_core.set_message_id(session, row.id, 321)

    async with _Harness(sessionmaker, clock=clock) as h:
        original = h.fake.make_request

        async def failing(bot, method, timeout=None):
            if isinstance(method, EditMessageText):
                raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
            return await original(bot, method, timeout)

        h.fake.make_request = failing
        resp = await h.post(_body())
        assert resp.status == 202
        assert "checkin" in _invalidates(h.hub)
    assert len(await _web_rows(sessionmaker)) == 1


# --- GET /api/checkin ---------------------------------------------------------


async def test_get_checkin_shape_and_no_forbidden_keys(sessionmaker):
    await _seed(sessionmaker, due_action="сдать отчёт", streak=3)
    order_id = await _add_order(sessionmaker, "пить воду")
    async with _Harness(sessionmaker) as h:
        body = await (await h.get("/api/checkin")).json()
        assert body == {
            "local_date": "2026-01-01",
            "streak": 3,
            "last_checkin_at": None,
            "done_today": False,
            "in_progress": False,
            "today": None,
            "form": {
                "due_action": "сдать отчёт",
                "orders": [{"id": order_id, "text": "пить воду"}],
                "note_max": 500,
            },
        }
        await h.post(
            _body(due_result="done", orders=[{"id": order_id, "result": "no"}], note="заметка")
        )
        body = await (await h.get("/api/checkin")).json()

    assert body["today"] == {
        "local_date": "2026-01-01",
        "rating": 4,
        "due_result": "done",
        "note": "заметка",
        "orders": [{"text": "пить воду", "result": "no"}],
    }
    raw = json.dumps(body)
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in raw


async def test_get_checkin_done_today_follows_last_checkin_at(sessionmaker):
    await _seed(sessionmaker, last_checkin_at=START - datetime.timedelta(hours=1), streak=1)
    async with _Harness(sessionmaker) as h:
        body = await (await h.get("/api/checkin")).json()
    assert body["done_today"] is True
    assert body["last_checkin_at"].startswith("2026-01-01T11:00:00")


# --- GET /api/checkins --------------------------------------------------------


async def test_get_checkins_returns_the_window_ascending(sessionmaker):
    await _seed(sessionmaker)
    order_id = await _add_order(sessionmaker, "пить воду")
    async with sessionmaker() as session:
        for offset, rating in ((0, 5), (2, 3), (29, 1), (30, 2)):
            session.add(
                Checkin(
                    local_date=DAY - datetime.timedelta(days=offset),
                    day_rating=rating,
                    due_result="none",
                    note=f"n{offset}",
                    tg_message_id=10 + offset,
                )
            )
        await session.commit()
        today = (
            await session.execute(select(Checkin).where(Checkin.local_date == DAY))
        ).scalar_one()
        session.add(CheckinOrderResult(checkin_id=today.id, order_id=order_id, result="done"))
        await session.commit()

    async with _Harness(sessionmaker) as h:
        body = await (await h.get("/api/checkins")).json()
        assert body["days"] == 30
        assert body["from"] == "2025-12-03"
        assert body["to"] == "2026-01-01"
        assert [item["local_date"] for item in body["items"]] == [
            "2025-12-03",
            "2025-12-30",
            "2026-01-01",
        ]
        assert body["items"][-1] == {
            "local_date": "2026-01-01",
            "rating": 5,
            "due_result": "none",
            "note": "n0",
            "orders": [{"text": "пить воду", "result": "done"}],
        }
        assert body["items"][0]["orders"] == []
        raw = json.dumps(body)
        for key in FORBIDDEN_KEYS + ("id",):
            assert f'"{key}"' not in raw

        body = await (await h.get("/api/checkins?days=1")).json()
        assert [item["local_date"] for item in body["items"]] == ["2026-01-01"]
        body = await (await h.get("/api/checkins?days=90")).json()
        assert len(body["items"]) == 4


@pytest.mark.parametrize("days", ["0", "91", "-1", "abc", "", "1.5", "٣", "1000000"])
async def test_get_checkins_rejects_bad_days(sessionmaker, days):
    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        resp = await h.get(f"/api/checkins?days={days}")
        assert resp.status == 400
        assert (await resp.json()) == {"error": "bad_request"}


# --- GET /api/journal ---------------------------------------------------------


async def test_get_journal_pages_newest_first(sessionmaker):
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        for i in range(5):
            session.add(Journal(local_date=DAY - datetime.timedelta(days=i // 2), text=f"строка {i}"))
        await session.commit()

    async with _Harness(sessionmaker) as h:
        body = await (await h.get("/api/journal?limit=2")).json()
        assert body["total"] == 5
        assert [item["text"] for item in body["items"]] == ["строка 1", "строка 0"]
        assert set(body["items"][0]) == {"id", "local_date", "text", "created_at"}
        body = await (await h.get("/api/journal?offset=2&limit=2")).json()
        assert [item["text"] for item in body["items"]] == ["строка 3", "строка 2"]
        body = await (await h.get("/api/journal?offset=99999999999999999999&limit=1000")).json()
        assert body["items"] == [] and body["total"] == 5
        body = await (await h.get("/api/journal?offset=x&limit=y")).json()
        assert len(body["items"]) == 5


# --- logs ---------------------------------------------------------------------


async def test_logs_never_contain_the_note_or_order_text(sessionmaker, caplog):
    await _seed(sessionmaker)
    order_id = await _add_order(sessionmaker, "СЕКРЕТНОЕ_ДЕЛО")
    async with sessionmaker() as session:
        row = await checkin_core.start(session, FrozenClock(START), TIMEZONE)
        await checkin_core.set_message_id(session, row.id, 321)
    async with _Harness(sessionmaker) as h:
        with caplog.at_level(logging.DEBUG):
            resp = await h.post(
                _body(orders=[{"id": order_id, "result": "done"}], note="СЕКРЕТНАЯ_ЗАМЕТКА")
            )
            assert resp.status == 202
            await h.get("/api/checkin")
            await h.get("/api/checkins")
            resp = await h.post(_body(note="СЕКРЕТНАЯ \ud800"))
            assert resp.status == 400
    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "СЕКРЕТНАЯ" not in blob
    assert "СЕКРЕТНОЕ_ДЕЛО" not in blob


# --- through the worker -------------------------------------------------------


def _dp(sessionmaker, settings, provider, clock) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, settings, provider, None, clock))
    return dp


def _hub_messages(hub: WebHub) -> list[str]:
    return [e.data["text"] for e in _events(hub) if e.event == "message"]


@pytest.mark.parametrize("note", ["устал, но сделал", None])
async def test_the_worker_finishes_the_checkin_with_one_llm_call(sessionmaker, note):
    """The queued update through the real router/worker: exactly one
    model call, the check-in finished, the streak moved, and the reply
    published to the hub -- never sent to Telegram. A FrozenClock far
    from the real date proves the skip path uses the router's clock."""
    await _seed(sessionmaker)
    provider = FakeLLMProvider(text="Принято, отдыхай.")
    async with _Harness(sessionmaker) as h:
        assert (await h.post(_body(rating=3, note=note))).status == 202
        dp = _dp(sessionmaker, h.settings, provider, h.clock)
        assert await process_one_update(sessionmaker, dp, h.bot, h.clock, h.web_bot) is True
        assert await process_one_update(sessionmaker, dp, h.bot, h.clock, h.web_bot) is False

        assert provider.calls == 1
        assert _hub_messages(h.hub) == ["Принято, отдыхай."]
        assert len(h.fake.sent) == h.sent_after_login, "nothing sent to Telegram"
        assert all(e.message_id > 0 for e in h.fake.edits)

    (row,) = await _checkins(sessionmaker)
    assert row.note == note
    assert row.tg_message_id is None, "the completion consumed the minted id"
    state = await _state(sessionmaker)
    assert state.streak == 1
    assert state.last_checkin_at == START
    assert state.awaiting is None
    rows = await _web_rows(sessionmaker)
    assert [r.status for r in rows] == ["done"]
    async with sessionmaker() as session:
        user_rows = list(
            (await session.execute(select(Message).where(Message.role == "user"))).scalars()
        )
    assert [r.kind for r in user_rows] == ["checkin"]
    assert user_rows[0].content == checkin_core.synthetic_line(row)


async def test_a_replayed_skip_does_not_run_a_second_turn(sessionmaker):
    await _seed(sessionmaker)
    provider = FakeLLMProvider(text="Принято.")
    async with _Harness(sessionmaker) as h:
        assert (await h.post(_body())).status == 202
        (queued,) = await _web_rows(sessionmaker)
        dp = _dp(sessionmaker, h.settings, provider, h.clock)
        await process_one_update(sessionmaker, dp, h.bot, h.clock, h.web_bot)
        # The same synthetic press again (a replay under a fresh id).
        async with sessionmaker() as session:
            payload = dict(queued.payload)
            payload["update_id"] = queued.update_id - 1000
            session.add(TelegramUpdate(update_id=payload["update_id"], payload=payload))
            await session.commit()
        await process_one_update(sessionmaker, dp, h.bot, h.clock, h.web_bot)
    assert provider.calls == 1
    assert (await _state(sessionmaker)).streak == 1


async def test_a_pause_word_note_does_not_finish_the_checkin(sessionmaker):
    await _seed(sessionmaker)
    provider = FakeLLMProvider(text="не должно вызваться")
    async with _Harness(sessionmaker) as h:
        assert (await h.post(_body(note="пурпурный"))).status == 202
        dp = _dp(sessionmaker, h.settings, provider, h.clock)
        await process_one_update(sessionmaker, dp, h.bot, h.clock, h.web_bot)
        assert len(h.fake.sent) == h.sent_after_login

    assert provider.calls == 0
    state = await _state(sessionmaker)
    assert state.persona_active is False
    assert state.awaiting is None
    assert state.streak == 0
    assert state.last_checkin_at is None
    (row,) = await _checkins(sessionmaker)
    assert row.note is None


def _telegram_text(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "T"},
            "text": text,
        },
    }


@pytest.mark.parametrize("note", ["моя заметка", None])
async def test_a_message_queued_before_the_submit_is_never_taken_as_the_note(sessionmaker, note):
    """Review fix: a Telegram text already queued (sent while the worker
    was busy) when the form is submitted is claimed first. It must stay
    an ordinary chat turn -- not be filed as the check-in's note -- and
    the check-in must still finish with the form's own note."""
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=500, payload=_telegram_text(500, "постороннее")))
        await session.commit()
    provider = FakeLLMProvider(text="Ок.")
    async with _Harness(sessionmaker) as h:
        assert (await h.post(_body(rating=3, note=note))).status == 202
        dp = _dp(sessionmaker, h.settings, provider, h.clock)
        assert await process_one_update(sessionmaker, dp, h.bot, h.clock, h.web_bot) is True
        (row,) = await _checkins(sessionmaker)
        assert row.note == note, "the queued Telegram text was not filed as the note"
        assert (await _state(sessionmaker)).streak == 0, "not finished by the unrelated text"
        assert await process_one_update(sessionmaker, dp, h.bot, h.clock, h.web_bot) is True

    assert provider.calls == 2
    (row,) = await _checkins(sessionmaker)
    assert row.note == note
    assert (await _state(sessionmaker)).streak == 1
    async with sessionmaker() as session:
        user_rows = list(
            (
                await session.execute(
                    select(Message).where(Message.role == "user").order_by(Message.id)
                )
            ).scalars()
        )
    assert [(r.kind, r.content) for r in user_rows] == [
        ("chat", "постороннее"),
        ("checkin", checkin_core.synthetic_line(row)),
    ]


async def test_a_failed_enqueue_is_a_500_that_leaves_no_note_step_open(sessionmaker, monkeypatch, caplog):
    """Review fix: the core steps commit one by one, so a failure while
    queueing the completion leaves a filled row with nothing queued. It
    must not leave the note step open (the next chat message would be
    swallowed as the note), must not count as in_progress, must not log
    the note, and a resubmit must work."""
    from app.web import ingress

    await _seed(sessionmaker)
    real = ingress.checkin_complete

    async def failing(*args, **kwargs):
        raise RuntimeError("СЕКРЕТНАЯ_ЗАМЕТКА in a driver message")

    monkeypatch.setattr(ingress, "checkin_complete", failing)
    async with _Harness(sessionmaker) as h:
        with caplog.at_level(logging.DEBUG):
            resp = await h.post(_body(note="СЕКРЕТНАЯ_ЗАМЕТКА"))
        assert resp.status == 500
        assert (await resp.json()) == {"error": "internal"}
        state = await _state(sessionmaker)
        assert state.awaiting is None
        assert await _web_rows(sessionmaker) == []
        assert (await (await h.get("/api/checkin")).json())["in_progress"] is False

        monkeypatch.setattr(ingress, "checkin_complete", real)
        assert (await h.post(_body(note="вторая попытка"))).status == 202
    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "СЕКРЕТНАЯ" not in blob
    (row,) = await _checkins(sessionmaker)
    assert row.note == "вторая попытка"
    assert len(await _web_rows(sessionmaker)) == 1


async def test_concurrent_submits_queue_only_one_completion(sessionmaker):
    """Review fix: the in-progress check and the enqueue run under one
    lock, so two simultaneous submits (two tabs) cannot both pass it."""
    import asyncio

    await _seed(sessionmaker)
    async with _Harness(sessionmaker) as h:
        first, second = await asyncio.gather(
            h.post(_body(rating=2, note="а")), h.post(_body(rating=5, note="б"))
        )
        assert sorted([first.status, second.status]) == [202, 409]
    assert len(await _web_rows(sessionmaker)) == 1


async def test_a_web_redo_asks_only_unanswered_orders_within_the_cap(sessionmaker):
    """Review fix (parity with Telegram's redo): the day's earlier order
    answers are kept, so a redo neither re-asks them nor goes past
    ORDERS_IN_CHECKIN_MAX."""
    await _seed(sessionmaker)
    once = await _add_order(sessionmaker, "разово", cadence="once")
    daily_a = await _add_order(sessionmaker, "ежедневно а")
    await _add_order(sessionmaker, "ежедневно б")
    settings = _settings(ORDERS_IN_CHECKIN_MAX=2)
    provider = FakeLLMProvider(text="Ок.")
    async with _Harness(sessionmaker, settings=settings) as h:
        body = await (await h.get("/api/checkin")).json()
        assert [o["id"] for o in body["form"]["orders"]] == [once, daily_a]
        orders = [{"id": once, "result": "done"}, {"id": daily_a, "result": "no"}]
        assert (await h.post(_body(orders=orders))).status == 202
        dp = _dp(sessionmaker, h.settings, provider, h.clock)
        assert await process_one_update(sessionmaker, dp, h.bot, h.clock, h.web_bot) is True

        body = await (await h.get("/api/checkin")).json()
        assert body["form"]["orders"] == []
        stale = [{"id": daily_a, "result": "done"}]
        resp = await h.post(_body(orders=stale))
        assert resp.status == 422
        assert (await h.post(_body(rating=5, orders=[]))).status == 202

    (row,) = await _checkins(sessionmaker)
    async with sessionmaker() as session:
        results = await orders_core.results_for_checkin(session, row.id)
    assert results == [("разово", "done"), ("ежедневно а", "no")]


async def test_a_forged_web_completion_cannot_finish_a_telegram_checkin(sessionmaker):
    """`c:n:web` finishes only a web-minted (negative) id."""
    await _seed(sessionmaker)
    clock = FrozenClock(START)
    async with sessionmaker() as session:
        row = await checkin_core.start(session, clock, TIMEZONE)
        await checkin_core.set_rating(session, row.id, 3)
        await checkin_core.set_message_id(session, row.id, 77)
    provider = FakeLLMProvider(text="Ок.")
    payload = {
        "update_id": 900,
        "callback_query": {
            "id": "cb900",
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "T"},
            "chat_instance": "ci",
            "data": checkin_ui.WEB_SUBMIT_CALLBACK,
            "message": {
                "message_id": 77,
                "date": 0,
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "…",
            },
        },
    }
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=900, payload=payload))
        await session.commit()
    fake_bot, _fake = make_bot()
    dp = _dp(sessionmaker, _settings(), provider, clock)
    await dp.feed_update(fake_bot, Update.model_validate(payload, context={"bot": fake_bot}))
    assert provider.calls == 0
    assert (await _state(sessionmaker)).streak == 0


# --- parity with the Telegram flow ---------------------------------------------


async def _telegram_flow(sessionmaker, settings, clock, order_id: int, note: str | None):
    fake_bot, fake = make_bot()
    dp = _dp(sessionmaker, settings, FakeLLMProvider(text="Ок."), clock)

    def command(update_id, text):
        return {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 0,
                "chat": {"id": CHAT_ID, "type": "private"},
                "from": {"id": CHAT_ID, "is_bot": False, "first_name": "T"},
                "text": text,
            },
        }

    def press(update_id, data):
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb{update_id}",
                "from": {"id": CHAT_ID, "is_bot": False, "first_name": "T"},
                "chat_instance": "ci",
                "data": data,
                "message": {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": CHAT_ID, "type": "private"},
                    "text": "…",
                },
            },
        }

    steps = [
        command(1, "/checkin"),
        press(2, "c:r:4"),
        press(3, "c:d:partial"),
        press(4, f"c:o:{order_id}:d"),
        command(5, note) if note else press(5, "c:n:skip"),
    ]
    async with sessionmaker() as session:
        for step in steps:
            session.add(TelegramUpdate(update_id=step["update_id"], payload=step))
        await session.commit()
    for step in steps:
        await dp.feed_update(fake_bot, Update.model_validate(step, context={"bot": fake_bot}))


async def _web_flow(sessionmaker, settings, clock, order_id: int, note: str | None):
    async with _Harness(sessionmaker, clock=clock, settings=settings) as h:
        resp = await h.post(
            _body(
                rating=4,
                due_result="partial",
                orders=[{"id": order_id, "result": "done"}],
                note=note,
            )
        )
        assert resp.status == 202
        dp = _dp(sessionmaker, settings, FakeLLMProvider(text="Ок."), clock)
        assert await process_one_update(sessionmaker, dp, h.bot, clock, h.web_bot)


@pytest.mark.parametrize("note", ["устал, но сделал", None])
@pytest.mark.parametrize("transport", ["telegram", "web"])
async def test_telegram_and_web_produce_the_same_checkin(sessionmaker, transport, note):
    """Same answers, either transport: the same row, streak and
    synthetic line (the stored check-in message). Parametrized so each
    transport is compared against the one expected outcome."""
    await _seed(sessionmaker, due_action="сдать отчёт")
    order_id = await _add_order(sessionmaker, "пить воду")
    clock = SystemClock()
    settings = _settings()
    flow = _telegram_flow if transport == "telegram" else _web_flow
    await flow(sessionmaker, settings, clock, order_id, note)

    (row,) = await _checkins(sessionmaker)
    assert (row.local_date, row.day_rating, row.due_result, row.note) == (
        clock_local_date(clock, TIMEZONE),
        4,
        "partial",
        note,
    )
    state = await _state(sessionmaker)
    assert state.streak == 1
    assert state.awaiting is None
    async with sessionmaker() as session:
        results = await orders_core.results_for_checkin(session, row.id)
        stored = list(
            (await session.execute(select(Message).where(Message.kind == "checkin"))).scalars()
        )
    assert results == [("пить воду", "done")]
    expected_line = "[чек-ин] день 4/5 · действие: частично · " + (
        f"«{note}» · " if note else ""
    ) + "договорённости: «пить воду» — да"
    assert [m.content for m in stored if m.role == "user"] == [expected_line]
