"""Webhook route tests (plan section 16).

- bad secret -> 403
- foreign chat.id -> 200, zero rows stored
- group chat -> 200, zero rows stored
- allowed private chat -> 200, exactly one row
- same update_id twice -> still one row (dedup)
"""

from __future__ import annotations

import datetime

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import HeartbeatState, TelegramUpdate
from app.tg.webhook import SECRET_HEADER, handle_webhook, healthz, heartbeat_stale, readyz

ALLOWED_CHAT_ID = 555
SECRET = "test-secret-token-123"


def _settings(database_url: str) -> Settings:
    return Settings(
        MODE="webhook",
        TELEGRAM_BOT_TOKEN="123456:TEST",
        TELEGRAM_SECRET_TOKEN=SECRET,
        ALLOWED_CHAT_ID=ALLOWED_CHAT_ID,
        PUBLIC_URL="https://example.invalid",
        DATABASE_URL=database_url,
    )


def _build_app(settings: Settings, sessionmaker, clock=None) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["sessionmaker"] = sessionmaker
    if clock is not None:
        app["clock"] = clock
    app.router.add_post("/telegram/webhook", handle_webhook)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    return app


def _private_message_update(update_id: int, chat_id: int = ALLOWED_CHAT_ID) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Test"},
            "text": "hello",
        },
    }


def _group_message_update(update_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": -100123, "type": "group"},
            "from": {"id": 999, "is_bot": False, "first_name": "Someone"},
            "text": "hello group",
        },
    }


async def _row_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        result = await session.execute(select(func.count()).select_from(TelegramUpdate))
        return result.scalar_one()


async def test_wrong_secret_returns_403(test_database_url, sessionmaker):
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/telegram/webhook",
            json=_private_message_update(1),
            headers={SECRET_HEADER: "wrong-secret"},
        )
        assert resp.status == 403
    assert await _row_count(sessionmaker) == 0


async def test_missing_secret_returns_403(test_database_url, sessionmaker):
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/telegram/webhook", json=_private_message_update(2))
        assert resp.status == 403
    assert await _row_count(sessionmaker) == 0


async def test_foreign_chat_returns_200_and_stores_nothing(test_database_url, sessionmaker):
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/telegram/webhook",
            json=_private_message_update(3, chat_id=999999),
            headers={SECRET_HEADER: SECRET},
        )
        assert resp.status == 200
    assert await _row_count(sessionmaker) == 0


async def test_group_chat_returns_200_and_stores_nothing(test_database_url, sessionmaker):
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/telegram/webhook",
            json=_group_message_update(4),
            headers={SECRET_HEADER: SECRET},
        )
        assert resp.status == 200
    assert await _row_count(sessionmaker) == 0


async def test_allowed_private_chat_stores_exactly_one_row(test_database_url, sessionmaker):
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/telegram/webhook",
            json=_private_message_update(5),
            headers={SECRET_HEADER: SECRET},
        )
        assert resp.status == 200
    assert await _row_count(sessionmaker) == 1


async def test_duplicate_update_id_inserts_once(test_database_url, sessionmaker):
    app = _build_app(_settings(test_database_url), sessionmaker)
    payload = _private_message_update(6)
    async with TestClient(TestServer(app)) as client:
        for _ in range(2):
            resp = await client.post(
                "/telegram/webhook", json=payload, headers={SECRET_HEADER: SECRET}
            )
            assert resp.status == 200
    assert await _row_count(sessionmaker) == 1


async def test_healthz_always_200(test_database_url, sessionmaker):
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200


async def _upsert_heartbeat_at(sessionmaker, heartbeat_at) -> None:
    """Set heartbeat_state.id=1's stamp, inserting the row if the per-
    test TRUNCATE (tests/conftest.py's sessionmaker fixture) removed it
    -- the migration only ever inserts it once, when the database is
    first created."""
    async with sessionmaker() as session:
        await session.execute(
            pg_insert(HeartbeatState).values(id=1).on_conflict_do_nothing(index_elements=["id"])
        )
        await session.execute(
            sql_update(HeartbeatState).where(HeartbeatState.id == 1).values(heartbeat_at=heartbeat_at)
        )
        await session.commit()


async def test_readyz_200_when_db_reachable_and_heartbeat_fresh(test_database_url, sessionmaker):
    """6e: /readyz now also checks heartbeat_state -- a fresh stamp is
    required for 200, not just a reachable database (plan section 9.6)."""
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    await _upsert_heartbeat_at(sessionmaker, now)

    clock = FrozenClock(now + datetime.timedelta(minutes=1))
    app = _build_app(_settings(test_database_url), sessionmaker, clock=clock)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/readyz")
        assert resp.status == 200


async def test_readyz_503_when_heartbeat_never_ran(test_database_url, sessionmaker):
    """The migration inserts heartbeat_state with heartbeat_at=null --
    a fresh deploy before the heartbeat's first tick is not ready."""
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/readyz")
        assert resp.status == 503


async def test_readyz_503_when_heartbeat_stale(test_database_url, sessionmaker):
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    await _upsert_heartbeat_at(sessionmaker, now)

    settings = _settings(test_database_url)
    clock = FrozenClock(now + datetime.timedelta(minutes=settings.LIVENESS_STALE_MIN + 1))
    app = _build_app(settings, sessionmaker, clock=clock)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/readyz")
        assert resp.status == 503


def test_heartbeat_stale_pure():
    """The predicate itself, table-driven (plan section 12's "/readyz
    fails when the heartbeat is stale")."""
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    assert heartbeat_stale(None, now, 5) is True
    assert heartbeat_stale(now - datetime.timedelta(minutes=4), now, 5) is False
    assert heartbeat_stale(now - datetime.timedelta(minutes=5, seconds=1), now, 5) is True


# --- 2b: callback_query updates (the first inline keyboards) ---


def _private_callback_update(update_id: int = 700) -> dict:
    """A button press. The allow-list keys on callback_query.message.chat,
    which extract_chat already falls back to -- these tests pin that
    behaviour now that something actually sends keyboards."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb1",
            "from": {"id": ALLOWED_CHAT_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": "m:p:20",
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": ALLOWED_CHAT_ID, "type": "private"},
                "text": "…",
            },
        },
    }


def _foreign_callback_update(update_id: int = 701) -> dict:
    payload = _private_callback_update(update_id)
    payload["callback_query"]["message"]["chat"]["id"] = 999999
    return payload


def _group_callback_update(update_id: int = 702) -> dict:
    payload = _private_callback_update(update_id)
    payload["callback_query"]["message"]["chat"] = {"id": -100123, "type": "group"}
    return payload


async def test_allowed_private_callback_stores_exactly_one_row(
    test_database_url, sessionmaker
):
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/telegram/webhook",
            json=_private_callback_update(),
            headers={SECRET_HEADER: SECRET},
        )
        assert resp.status == 200
    assert await _row_count(sessionmaker) == 1


async def test_foreign_chat_callback_returns_200_and_stores_nothing(
    test_database_url, sessionmaker
):
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/telegram/webhook",
            json=_foreign_callback_update(),
            headers={SECRET_HEADER: SECRET},
        )
        assert resp.status == 200
    assert await _row_count(sessionmaker) == 0


async def test_group_callback_returns_200_and_stores_nothing(test_database_url, sessionmaker):
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/telegram/webhook",
            json=_group_callback_update(),
            headers={SECRET_HEADER: SECRET},
        )
        assert resp.status == 200
    assert await _row_count(sessionmaker) == 0
