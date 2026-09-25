"""app/web/panels/memory.py end to end (W3): GET /api/memories and the
add/edit/pin/unpin/forget endpoints, exercised through a real aiohttp
TestClient against setup_web's routes -- the same shape
tests/test_web_panel_state.py and tests/test_web_panel_proposals.py
already use.

Covers: 401/403/429, validation (bad_kind, empty, too_long, control
characters, a non-int/negative id), add + duplicate -> 409 carrying
`existing`, edit -> a new row with the old one superseded and absent
from the list, the pin cap -> 409, forget -> 404 the second time, the
audit row (source="web", no text anywhere), the DTO's shape (no
confidence, no superseded rows), that invalidate("memory")/
invalidate("state") both publish on every write, that clear_awaiting
runs, and that nothing here ever sends anything to Telegram.
"""

from __future__ import annotations

import datetime
import json
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.config import Settings
from app.core import memory as memory_core
from app.core.clock import FrozenClock
from app.db.models import Memory, StateChange, StudyCard, StudyClip, StudyJob, UserState
from app.web.hub import WebHub
from app.web.ratelimit import WebRateLimiter
from app.web.routes import setup_web
from app.web.sink import make_web_bot
from conftest import FakeSession, make_bot
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
    sent_text = fake_session.sent[-1].text
    import re

    code = re.search(r"[0-9A-Z]{4}-[0-9A-Z]{4}", sent_text).group(0)
    resp = await _post(
        client, "/api/auth/code", {"code": code}, cookies={"__Host-anchor_pre": pre_token}
    )
    assert resp.status == 200
    return {"__Host-anchor_s": _cookie(resp, "__Host-anchor_s")}


async def _seed_state(sessionmaker, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()


async def _write(sessionmaker, **kwargs) -> Memory:
    async with sessionmaker() as session:
        return await memory_core.write_memory(session, **kwargs)


async def _changes(sessionmaker) -> list[StateChange]:
    async with sessionmaker() as session:
        return list((await session.execute(select(StateChange))).scalars())


async def _all_memories(sessionmaker) -> list[Memory]:
    async with sessionmaker() as session:
        return list((await session.execute(select(Memory))).scalars())


# --- GET /api/memories -------------------------------------------------


async def test_memories_requires_a_session(sessionmaker):
    bot, _fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        resp = await _get(client, "/api/memories")
        assert resp.status == 401
        assert (await resp.json()) == {"error": "unauthenticated"}


async def test_memories_rejects_a_foreign_origin(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(
            client,
            "/api/memories",
            cookies=cookies,
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        assert resp.status == 403


async def test_memories_empty_shape(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(MEMORY_PINNED_MAX=8), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/memories", cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
    assert body == {"items": [], "total": 0, "pinned_count": 0, "pinned_max": 8}


async def test_memories_dto_shape_has_no_forbidden_fields(sessionmaker):
    await _seed_state(sessionmaker)
    await _write(sessionmaker, kind="identity", text="живёт в Лилле", source="user")
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/memories", cookies=cookies)
        body = await resp.json()

    assert body["total"] == 1
    item = body["items"][0]
    assert set(item) == {
        "id", "kind", "kind_label", "text", "pinned", "source",
        "use_count", "last_used_at", "created_at", "has_predecessor",
    }
    assert item["kind"] == "identity"
    assert item["kind_label"] == memory_core.KIND_LABEL["identity"]
    assert item["text"] == "живёт в Лилле"
    assert item["source"] == "user"
    assert item["pinned"] is False
    assert item["use_count"] == 0
    assert item["has_predecessor"] is False


async def test_memories_excludes_superseded_rows_and_confidence(sessionmaker):
    await _seed_state(sessionmaker)
    old = await _write(sessionmaker, kind="identity", text="живёт в Лилле", source="user")
    async with sessionmaker() as session:
        await memory_core.write_memory(
            session, kind="identity", text="живёт в Руане", source="user", supersedes_id=old.id
        )
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/memories", cookies=cookies)
        body = await resp.json()

    assert body["total"] == 1
    assert body["items"][0]["text"] == "живёт в Руане"
    raw = json.dumps(body)
    assert "confidence" not in raw
    assert "Лилле" not in raw


async def test_memories_has_predecessor_marks_chain_heads(sessionmaker):
    """W3 finding, revised by 8c (section 18.1): forgetting a chain head
    with a predecessor forgets the predecessor too. The confirm dialog
    says so using `has_predecessor`, which must be true for an edited
    row's replacement and false for anything else."""
    await _seed_state(sessionmaker)
    old = await _write(sessionmaker, kind="identity", text="живёт в Лилле", source="user")
    async with sessionmaker() as session:
        await memory_core.write_memory(
            session, kind="identity", text="живёт в Руане", source="user", supersedes_id=old.id
        )
    await _write(sessionmaker, kind="event", text="не связано", source="user")
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/memories", cookies=cookies)
        body = await resp.json()

    by_text = {i["text"]: i["has_predecessor"] for i in body["items"]}
    assert by_text == {"живёт в Руане": True, "не связано": False}


async def test_memories_filters_by_kind_and_pinned(sessionmaker):
    await _seed_state(sessionmaker)
    await _write(sessionmaker, kind="rule", text="правило", source="user", pinned=True)
    await _write(sessionmaker, kind="event", text="событие", source="user")
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/memories?kind=rule", cookies=cookies)
        body = await resp.json()
        assert [i["kind"] for i in body["items"]] == ["rule"]

        resp = await _get(client, "/api/memories?pinned=true", cookies=cookies)
        body = await resp.json()
        assert [i["kind"] for i in body["items"]] == ["rule"]
        assert body["pinned_count"] == 1


async def test_memories_rejects_a_bad_kind_filter(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/memories?kind=nonsense", cookies=cookies)
        assert resp.status == 400


async def test_memories_caps_the_limit_at_50(sessionmaker):
    await _seed_state(sessionmaker)
    async with sessionmaker() as session:
        for i in range(60):
            session.add(Memory(kind="event", text=f"факт {i}", source="user"))
        await session.commit()
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/memories?limit=1000", cookies=cookies)
        body = await resp.json()
    assert len(body["items"]) == 50
    assert body["total"] == 60


async def test_memories_an_offset_past_bigint_is_clamped_not_500(sessionmaker):
    """W3 finding: `_parse_positive_int` has no upper bound, so an
    offset like this used to reach Postgres's OFFSET clause and raise
    an unhandled 500 instead of just being a page past the end."""
    await _seed_state(sessionmaker)
    await _write(sessionmaker, kind="identity", text="факт", source="user")
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _get(client, "/api/memories?offset=99999999999999999999", cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
    assert body["items"] == []
    assert body["total"] == 1


# --- POST /api/memories (add) -------------------------------------------


async def test_post_memory_adds_with_source_user(sessionmaker):
    await _seed_state(sessionmaker, awaiting="checkin_note", awaiting_ref=1)
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client, "/api/memories", {"kind": "identity", "text": "  живёт в Лилле  "}, cookies=cookies
        )
        assert resp.status == 201
        body = await resp.json()

    assert body["memory"]["text"] == "живёт в Лилле"  # stripped
    assert body["memory"]["kind"] == "identity"

    rows = await _all_memories(sessionmaker)
    assert len(rows) == 1
    assert rows[0].source == "user"

    async with sessionmaker() as session:
        state = await memory_core.get_active(session, rows[0].id)
    assert state is not None

    from app.core.state import get_state
    async with sessionmaker() as session:
        us = await get_state(session)
    assert us.awaiting is None and us.awaiting_ref is None  # clear_awaiting ran

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "memory" in topics and "state" in topics

    # Silent in Telegram: no message about the new memory was sent.
    assert all("Лилле" not in m.text for m in fake.sent)


async def test_post_memory_rejects_a_bad_kind(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/memories", {"kind": "nonsense", "text": "факт"}, cookies=cookies)
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "bad_kind"}


async def test_post_memory_rejects_empty_text(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/memories", {"kind": "identity", "text": "   "}, cookies=cookies)
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "empty"}


async def test_post_memory_rejects_text_over_the_max(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client,
            "/api/memories",
            {"kind": "identity", "text": "я" * (memory_core.MEMORY_TEXT_MAX + 1)},
            cookies=cookies,
        )
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "too_long"}


async def test_post_memory_rejects_control_characters(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client, "/api/memories", {"kind": "identity", "text": "факт\x00с NUL"}, cookies=cookies
        )
        assert resp.status == 400


async def test_post_memory_rejects_a_lone_surrogate(sessionmaker, caplog):
    """W3 finding: a lone UTF-16 surrogate passed `_valid_shape` before
    (it is not a control character), then failed asyncpg's UTF-8
    encoding at the query boundary -- an unhandled 500 whose
    aiohttp.server traceback logged the text as a SQL parameter repr.
    Rejected with 400 before ever reaching the database now, so nothing
    -- including the surrounding secret text -- reaches the logs."""
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        with caplog.at_level(logging.DEBUG):
            resp = await _post(
                client,
                "/api/memories",
                {"kind": "identity", "text": "SECRETTEXT \ud800 tail"},
                cookies=cookies,
            )
            assert resp.status == 400

    rows = await _all_memories(sessionmaker)
    assert rows == []
    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "SECRETTEXT" not in blob


async def test_post_edit_rejects_a_lone_surrogate(sessionmaker):
    await _seed_state(sessionmaker)
    row = await _write(sessionmaker, kind="identity", text="факт", source="user")
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client, f"/api/memories/{row.id}/edit", {"text": "текст \ud800"}, cookies=cookies
        )
        assert resp.status == 400


async def test_post_memory_rejects_a_non_string_kind_or_text(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/memories", {"kind": 1, "text": "факт"}, cookies=cookies)
        assert resp.status == 400
        resp = await _post(client, "/api/memories", {"kind": "identity", "text": 5}, cookies=cookies)
        assert resp.status == 400


async def test_post_memory_duplicate_returns_409_with_existing(sessionmaker):
    await _seed_state(sessionmaker)
    existing = await _write(
        sessionmaker, kind="identity", text="пользователь живёт в Лилле", source="user"
    )
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client,
            "/api/memories",
            {"kind": "identity", "text": "пользователь живёт в Лилле"},
            cookies=cookies,
        )
        assert resp.status == 409
        body = await resp.json()

    assert body["error"] == "duplicate"
    assert body["existing"]["id"] == existing.id
    assert body["existing"]["text"] == "пользователь живёт в Лилле"

    rows = await _all_memories(sessionmaker)
    assert len(rows) == 1  # nothing new written

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert topics == []  # a 409 write invalidates nothing


# --- POST /api/memories/{id}/edit ---------------------------------------


async def test_post_edit_supersedes_the_old_row(sessionmaker):
    await _seed_state(sessionmaker)
    old = await _write(sessionmaker, kind="identity", text="живёт в Лилле", source="user")
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client, f"/api/memories/{old.id}/edit", {"text": "живёт в Руане"}, cookies=cookies
        )
        assert resp.status == 200
        body = await resp.json()

        assert body["memory"]["text"] == "живёт в Руане"
        assert body["memory"]["kind"] == "identity"  # kind carried over from the old row
        assert body["memory"]["id"] != old.id

        resp = await _get(client, "/api/memories", cookies=cookies)
        listed = await resp.json()

    async with sessionmaker() as session:
        refreshed_old = await session.get(Memory, old.id)
    assert refreshed_old.superseded_by == body["memory"]["id"]

    texts = [i["text"] for i in listed["items"]]
    assert "живёт в Руане" in texts
    assert "живёт в Лилле" not in texts

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "memory" in topics and "state" in topics


async def test_post_edit_carries_pinned_forward(sessionmaker):
    """W3 finding: write_memory's own default (pinned=False) used to
    silently unpin every edited memory, since post_edit never passed
    the old row's pinned state through."""
    await _seed_state(sessionmaker)
    old = await _write(
        sessionmaker, kind="identity", text="живёт в Лилле", source="user", pinned=True
    )
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client, f"/api/memories/{old.id}/edit", {"text": "живёт в Руане"}, cookies=cookies
        )
        assert resp.status == 200
        body = await resp.json()

        resp = await _get(client, "/api/memories?pinned=true", cookies=cookies)
        listed = await resp.json()

    assert body["memory"]["pinned"] is True
    assert body["memory"]["has_predecessor"] is True
    assert listed["pinned_count"] == 1
    assert [i["id"] for i in listed["items"]] == [body["memory"]["id"]]


async def test_post_edit_a_missing_id_is_404(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/memories/999999/edit", {"text": "что-то"}, cookies=cookies)
        assert resp.status == 404
        assert (await resp.json()) == {"error": "not_found"}


async def test_post_edit_a_non_int_id_is_404_not_500(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/memories/abc/edit", {"text": "что-то"}, cookies=cookies)
        assert resp.status == 404


async def test_post_edit_a_negative_id_is_404(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/memories/-1/edit", {"text": "что-то"}, cookies=cookies)
        assert resp.status == 404


async def test_post_edit_rejects_too_long_text(sessionmaker):
    await _seed_state(sessionmaker)
    row = await _write(sessionmaker, kind="identity", text="факт", source="user")
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client,
            f"/api/memories/{row.id}/edit",
            {"text": "я" * (memory_core.MEMORY_TEXT_MAX + 1)},
            cookies=cookies,
        )
        assert resp.status == 422
        assert (await resp.json()) == {"error": "invalid", "detail": "too_long"}


async def test_post_edit_editing_an_already_superseded_row_is_404(sessionmaker):
    await _seed_state(sessionmaker)
    old = await _write(sessionmaker, kind="identity", text="живёт в Лилле", source="user")
    async with sessionmaker() as session:
        await memory_core.write_memory(
            session, kind="identity", text="живёт в Руане", source="user", supersedes_id=old.id
        )
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client, f"/api/memories/{old.id}/edit", {"text": "живёт в Марселе"}, cookies=cookies
        )
        assert resp.status == 404


async def test_post_edit_duplicate_returns_409(sessionmaker):
    await _seed_state(sessionmaker)
    row = await _write(sessionmaker, kind="identity", text="живёт в Лилле", source="user")
    other = await _write(
        sessionmaker, kind="identity", text="пользователь живёт в Лилле сейчас", source="user"
    )
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client,
            f"/api/memories/{row.id}/edit",
            {"text": "пользователь живёт в Лилле сейчас"},
            cookies=cookies,
        )
        assert resp.status == 409
        body = await resp.json()
    assert body["error"] == "duplicate"
    assert body["existing"]["id"] == other.id


# --- POST /api/memories/{id}/pin | unpin ---------------------------------


async def test_post_pin_and_unpin(sessionmaker):
    await _seed_state(sessionmaker)
    row = await _write(sessionmaker, kind="identity", text="факт", source="user")
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/memories/{row.id}/pin", {}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
        assert body["memory"]["pinned"] is True

        resp = await _post(client, f"/api/memories/{row.id}/unpin", {}, cookies=cookies)
        assert resp.status == 200
        body = await resp.json()
        assert body["memory"]["pinned"] is False

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert topics.count("memory") == 2 and topics.count("state") == 2


async def test_post_pin_a_missing_id_is_404(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/memories/999999/pin", {}, cookies=cookies)
        assert resp.status == 404


async def test_post_pin_over_cap_returns_409(sessionmaker):
    await _seed_state(sessionmaker)
    await _write(sessionmaker, kind="identity", text="пользователь живёт в Лилле", source="user", pinned=True)
    extra = await _write(
        sessionmaker, kind="event", text="по воскресеньям мы ездим к морю", source="user"
    )
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(MEMORY_PINNED_MAX=1), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/memories/{extra.id}/pin", {}, cookies=cookies)
        assert resp.status == 409
        body = await resp.json()
    assert body == {"error": "over_cap", "max": 1}

    async with sessionmaker() as session:
        assert (await session.get(Memory, extra.id)).pinned is False

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert topics == []  # a 409 write invalidates nothing


async def test_post_pin_re_pinning_at_the_cap_is_allowed(sessionmaker):
    await _seed_state(sessionmaker)
    row = await _write(sessionmaker, kind="identity", text="факт", source="user", pinned=True)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(MEMORY_PINNED_MAX=1), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/memories/{row.id}/pin", {}, cookies=cookies)
        assert resp.status == 200


# --- POST /api/memories/{id}/forget ---------------------------------------


async def test_post_forget_deletes_and_audits_source_web_without_text(sessionmaker):
    await _seed_state(sessionmaker)
    row = await _write(sessionmaker, kind="identity", text="пользователь живёт в Лилле", source="user")
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/memories/{row.id}/forget", {}, cookies=cookies)
        assert resp.status == 200
        assert (await resp.json()) == {}

    rows = await _all_memories(sessionmaker)
    assert rows == []

    changes = await _changes(sessionmaker)
    assert len(changes) == 1
    audit = changes[0]
    assert audit.field == "memory"
    assert audit.old_value == str(row.id)
    assert audit.new_value is None
    assert audit.source == "web"
    for value in (audit.old_value, audit.new_value):
        assert value is None or "Лилле" not in value

    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert "memory" in topics and "state" in topics

    assert all("Лилле" not in m.text for m in fake.sent)  # silent in Telegram


async def test_post_forget_a_second_time_is_404(sessionmaker):
    await _seed_state(sessionmaker)
    row = await _write(sessionmaker, kind="identity", text="факт", source="user")
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/memories/{row.id}/forget", {}, cookies=cookies)
        assert resp.status == 200
        resp = await _post(client, f"/api/memories/{row.id}/forget", {}, cookies=cookies)
        assert resp.status == 404
        assert (await resp.json()) == {"error": "not_found"}


async def test_post_forget_a_non_int_id_is_404_not_500(sessionmaker):
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, "/api/memories/0/forget", {}, cookies=cookies)
        assert resp.status == 404
        resp = await _post(client, "/api/memories/not-a-number/forget", {}, cookies=cookies)
        assert resp.status == 404


async def test_post_forget_an_id_past_bigint_is_404_not_500(sessionmaker):
    """W3 finding: an id this large used to reach Postgres's bigint id
    column and raise an unhandled 500 instead of the same 404 any other
    malformed id gets."""
    await _seed_state(sessionmaker)
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(
            client, "/api/memories/99999999999999999999/forget", {}, cookies=cookies
        )
        assert resp.status == 404
        resp = await _post(
            client, "/api/memories/99999999999999999999/pin", {}, cookies=cookies
        )
        assert resp.status == 404


async def test_post_forget_a_superseded_id_is_404_not_200(sessionmaker):
    """W3 finding: forget used to hard-delete a superseded id and
    return 200, even though edit/pin already 404 on the same id via
    get_active -- an existence oracle for hidden history, and a 200
    that told the user something was forgotten while its replacement
    (the row that actually carries the corrected fact) stayed active
    and untouched."""
    await _seed_state(sessionmaker)
    old = await _write(sessionmaker, kind="identity", text="живёт в Лилле", source="user")
    async with sessionmaker() as session:
        new = await memory_core.write_memory(
            session, kind="identity", text="живёт в Руане", source="user", supersedes_id=old.id
        )
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/memories/{old.id}/forget", {}, cookies=cookies)
        assert resp.status == 404
        assert (await resp.json()) == {"error": "not_found"}

    async with sessionmaker() as session:
        assert await session.get(Memory, old.id) is not None, "the superseded row must survive"
        assert await memory_core.get_active(session, new.id) is not None, (
            "the row that actually replaced it must stay untouched"
        )
    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert topics == []  # a 404 write invalidates nothing


async def test_post_forget_an_adopted_technique_is_409(sessionmaker):
    """W3 finding: forgetting the head of a chain a StudyCard still
    points at used to raise an unhandled IntegrityError. The endpoint
    must map app.core.memory's FORGET_PROTECTED to 409, not 500, and
    leave the row exactly as it was."""
    await _seed_state(sessionmaker)
    row = await _write(sessionmaker, kind="technique", text="дыши перед сном", source="adopt")
    async with sessionmaker() as session:
        job = StudyJob(kind="read", local_date=datetime.date(2026, 1, 1), status="done")
        session.add(job)
        await session.flush()
        clip = StudyClip(job_id=job.id, url="https://example.test/sleep", domain="example.test", text="т")
        session.add(clip)
        await session.flush()
        session.add(
            StudyCard(
                job_id=job.id,
                clip_id=clip.id,
                kind="technique",
                text="дыши перед сном",
                quote="q",
                source_url=clip.url,
                risk_model="low",
                risk_rules="low",
                risk_final="low",
                status="adopted",
                memory_id=row.id,
            )
        )
        await session.commit()
    bot, fake = make_bot()
    app, hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        resp = await _post(client, f"/api/memories/{row.id}/forget", {}, cookies=cookies)
        assert resp.status == 409
        assert (await resp.json()) == {"error": "adopted"}

    async with sessionmaker() as session:
        assert await session.get(Memory, row.id) is not None
    topics = [record.data["topic"] for record in hub._buffer if record.event == "invalidate"]
    assert topics == []  # a refused write invalidates nothing


# --- 429 -------------------------------------------------------------------


async def test_panel_write_rate_limited(sessionmaker):
    await _seed_state(sessionmaker)
    row = await _write(sessionmaker, kind="identity", text="факт", source="user")
    bot, fake = make_bot()
    app, _hub, _web_bot = _build_app(_settings(), sessionmaker, FrozenClock(START), bot)
    limiter: WebRateLimiter = app["web_rate_limiter"]
    async with TestClient(TestServer(app)) as client:
        cookies = await _log_in(client, fake)
        for _ in range(60):
            assert limiter.check_panel_write() is None
        resp = await _post(client, f"/api/memories/{row.id}/pin", {}, cookies=cookies)
        assert resp.status == 429
        body = await resp.json()
        assert body["error"] == "rate_limited"
        assert resp.headers.get("Retry-After") is not None
