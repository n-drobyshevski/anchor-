"""Webhook route tests (plan section 16).

- bad secret -> 403
- foreign chat.id -> 200, zero rows stored
- group chat -> 200, zero rows stored
- allowed private chat -> 200, exactly one row
- same update_id twice -> still one row (dedup)
"""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import TelegramUpdate
from app.tg.webhook import SECRET_HEADER, handle_webhook, healthz, readyz

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


def _build_app(settings: Settings, sessionmaker) -> web.Application:
    app = web.Application()
    app["settings"] = settings
    app["sessionmaker"] = sessionmaker
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


async def test_readyz_200_when_db_reachable(test_database_url, sessionmaker):
    app = _build_app(_settings(test_database_url), sessionmaker)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/readyz")
        assert resp.status == 200
