"""The shared MCP core and `access_grant.client` (connector plan C1).

tests/test_grok_access.py pins Grok's behaviour and is not edited by
C1: it passing unchanged is the proof the extraction is a pure
refactor. This file pins what C1 adds for C2 to build on:

- a `Reader` whose tools/list follows `listed` and whose tools/call
  follows `grant`, so a connection's cached tool list and its window
  can differ;
- the tool-result refusal (`isError: true`), which neither reads nor
  counts nor notifies, next to Grok's unchanged `-32602`;
- one limiter per endpoint, keyed per caller;
- the column: every grant is Grok's, and the database refuses a Claude
  grant until C2 gives it somewhere to belong.
"""

from __future__ import annotations

import datetime
import json

import pytest
from aiogram import Bot
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.core import grants
from app.core.clock import SystemClock
from app.db.models import AccessGrant, Memory, UserState
from app.web import mcp, mcp_core
from conftest import FakeSession

CHAT_ID = 555
CLOSED = "Доступ закрыт. Открой его в Telegram: /claude"
NOTICE = "Тест прочитал: {what}"


async def _seed(sessionmaker) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone="Europe/Paris"))
        session.add(Memory(kind="identity", text="любит горы", source="user"))
        await session.commit()


async def _grant(sessionmaker, scopes=("memory",)) -> AccessGrant:
    async with sessionmaker() as session:
        _token, grant = await grants.create_grant(
            session, SystemClock(), scopes=list(scopes), ttl_hours=1
        )
    return grant


def _app(sessionmaker, reader_for, per_minute: int = 30):
    """An app with one core route; `reader_for()` builds the Reader."""
    fake = FakeSession()
    app = web.Application()
    app["settings"] = Settings(ALLOWED_CHAT_ID=CHAT_ID)
    app["sessionmaker"] = sessionmaker
    app["clock"] = SystemClock()
    app["bot"] = Bot(token="123456:TESTTOKEN", session=fake)
    limiter = mcp_core.RateLimiter(per_minute)

    async def handle(request):
        return await mcp_core.serve(request, reader_for(), limiter)

    app.router.add_route("*", "/core", handle)
    return app, fake


def _rpc(method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return body


async def _use_count(sessionmaker, grant_id: int) -> int:
    async with sessionmaker() as session:
        return (await session.get(AccessGrant, grant_id)).use_count


# --- Reader ---


async def test_tools_list_follows_listed_not_the_grant(sessionmaker):
    grant = await _grant(sessionmaker, ["memory"])
    reader = mcp_core.Reader(
        listed=("memory", "journal"), grant=grant, limit_key=1, notice=NOTICE
    )
    app, _ = _app(sessionmaker, lambda: reader)
    async with TestClient(TestServer(app)) as client:
        body = await (await client.post("/core", json=_rpc("tools/list"))).json()
    assert {t["name"] for t in body["result"]["tools"]} == {"get_memory", "get_journal"}


@pytest.mark.parametrize("has_grant", [False, True])
async def test_the_tool_result_refusal_reads_nothing(sessionmaker, has_grant):
    """No grant (no window), or a scope outside it: `isError`, not a 403."""
    await _seed(sessionmaker)
    grant = await _grant(sessionmaker, ["memory"])
    reader = mcp_core.Reader(
        listed=("memory", "journal"),
        grant=grant if has_grant else None,
        limit_key=1,
        notice=NOTICE,
        refusal=mcp_core.Refusal(CLOSED),
    )
    app, fake = _app(sessionmaker, lambda: reader)
    tool = "get_journal" if has_grant else "get_memory"
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/core", json=_rpc("tools/call", {"name": tool}))
        assert resp.status == 200
        body = await resp.json()
    assert "error" not in body
    assert body["result"] == {
        "content": [{"type": "text", "text": CLOSED}],
        "isError": True,
    }
    assert fake.sent == []
    assert await _use_count(sessionmaker, grant.id) == 0


async def test_the_rpc_refusal_is_unchanged(sessionmaker):
    grant = await _grant(sessionmaker, ["memory"])
    reader = mcp_core.Reader(
        listed=("memory",), grant=grant, limit_key=1, notice=NOTICE
    )
    app, fake = _app(sessionmaker, lambda: reader)
    async with TestClient(TestServer(app)) as client:
        body = await (
            await client.post("/core", json=_rpc("tools/call", {"name": "get_state"}))
        ).json()
    assert body["error"] == {"code": -32602, "message": "Unknown or not permitted tool"}
    assert fake.sent == []


@pytest.mark.parametrize(
    "params",
    [
        {"name": "drop_everything"},
        {"name": 7},
        {"name": "get_memory", "arguments": ["x"]},
    ],
)
async def test_an_unknown_tool_is_a_protocol_error_under_either_refusal(
    sessionmaker, params
):
    grant = await _grant(sessionmaker, ["memory"])
    reader = mcp_core.Reader(
        listed=("memory",),
        grant=grant,
        limit_key=1,
        notice=NOTICE,
        refusal=mcp_core.Refusal(CLOSED),
    )
    app, _ = _app(sessionmaker, lambda: reader)
    async with TestClient(TestServer(app)) as client:
        body = await (
            await client.post("/core", json=_rpc("tools/call", params))
        ).json()
    assert body["error"]["code"] == -32602


async def test_a_read_uses_the_readers_notice_and_instructions(sessionmaker):
    await _seed(sessionmaker)
    grant = await _grant(sessionmaker, ["memory"])
    reader = mcp_core.Reader(
        listed=("memory",),
        grant=grant,
        limit_key=1,
        notice=NOTICE,
        instructions="Только чтение.",
    )
    app, fake = _app(sessionmaker, lambda: reader)
    async with TestClient(TestServer(app)) as client:
        init = await (await client.post("/core", json=_rpc("initialize"))).json()
        body = await (
            await client.post("/core", json=_rpc("tools/call", {"name": "get_memory"}))
        ).json()
    assert init["result"]["instructions"] == "Только чтение."
    assert body["result"]["isError"] is False
    assert [
        m["text"] for m in json.loads(body["result"]["content"][0]["text"])["memories"]
    ] == ["любит горы"]
    assert [m.text for m in fake.sent] == ["Тест прочитал: память (1)"]
    assert await _use_count(sessionmaker, grant.id) == 1


async def test_the_core_and_grok_return_identical_payloads(sessionmaker):
    """Two routes onto one core: what a client reads does not depend on
    how it authenticated."""
    await _seed(sessionmaker)
    async with sessionmaker() as session:
        token, grant = await grants.create_grant(
            session, SystemClock(), scopes=["memory", "state"], ttl_hours=1
        )
    reader = mcp_core.Reader(
        listed=("memory", "state"), grant=grant, limit_key=1, notice=NOTICE
    )
    app, _ = _app(sessionmaker, lambda: reader)
    settings = Settings(
        GROK_ACCESS_ENABLED=True,
        MODE="webhook",
        PUBLIC_URL="https://anchor.example",
        ALLOWED_CHAT_ID=CHAT_ID,
    )
    app["settings"] = settings
    mcp.register(app, settings)
    async with TestClient(TestServer(app)) as client:
        for tool in ("get_memory", "get_state"):
            call = _rpc("tools/call", {"name": tool})
            via_core = await (await client.post("/core", json=call)).json()
            via_grok = await (await client.post(f"/mcp/{token}", json=call)).json()
            assert via_core == via_grok


# --- limiter ---


def test_limiters_are_independent_per_instance_and_per_key():
    grok, claude = mcp_core.RateLimiter(2), mcp_core.RateLimiter(2)
    assert [grok.allow(1, now=0.0) for _ in range(3)] == [True, True, False]
    assert claude.allow(1, now=0.0)
    assert grok.allow(2, now=0.0)
    assert grok.allow(1, now=61.0)


# --- the column ---


async def test_every_grant_is_grok_s(sessionmaker):
    grant = await _grant(sessionmaker)
    assert grant.client == "grok"
    async with sessionmaker() as session:
        await session.execute(
            text(
                "insert into access_grant (token_sha256, scopes, expires_at) "
                "values ('x', array['memory'], now() + interval '1 hour')"
            )
        )
        await session.commit()
        clients = (
            await session.execute(text("select distinct client from access_grant"))
        ).all()
    assert clients == [("grok",)]


@pytest.mark.parametrize(
    "client, token, constraint",
    [
        ("claude", "'x'", "ck_access_grant_client_token"),
        ("claude", "null", "token_sha256"),
        ("grok", "null", "token_sha256"),
        ("gemini", "'x'", "ck_access_grant_client"),
    ],
)
async def test_the_database_refuses_a_grant_c1_cannot_serve(
    sessionmaker, client, token, constraint
):
    """No Claude grant can exist before C2 builds what it belongs to."""
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError, match=constraint):
            await session.execute(
                text(
                    "insert into access_grant (client, token_sha256, scopes, expires_at) "
                    f"values (:client, {token}, array['memory'], :expires)"
                ),
                {"client": client, "expires": expires},
            )
