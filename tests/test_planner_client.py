"""app/planner/client.py: JSON-RPC over aiohttp, against a fake MCP server.

No `mcp` SDK, no real network -- `aiohttp.test_utils` (shipped with
aiohttp, already a dependency) stands in for the planner's `/api/mcp`.
"""

from __future__ import annotations

import asyncio
import datetime
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.db.models import PlannerCredential
from app.planner.client import ALLOWED_TOOLS, PlannerClient, PlannerToolError, PlannerUnavailable

pytestmark = pytest.mark.asyncio

TOKEN = "tok-1"


def _envelope(rpc_id, *, as_sse: bool) -> tuple[str, str]:
    body = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps({"events": [], "tasks": []})}
            ]
        },
    }
    if as_sse:
        return f"data: {json.dumps(body)}\n\n", "text/event-stream"
    return json.dumps(body), "application/json"


class _FakeMcpServer:
    """initialize / notifications/initialized / tools/call, minimally."""

    def __init__(self, *, sse: bool = False, fail_next_call: bool = False) -> None:
        self.calls: list[dict] = []
        self.session_id = "sess-1"
        self.sse = sse
        self.fail_next_call = fail_next_call

    async def handle(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.Response(status=401)
        body = await request.json()
        self.calls.append(body)
        method = body.get("method")

        if method == "initialize":
            return web.json_response(
                {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
                headers={"Mcp-Session-Id": self.session_id},
            )
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "tools/call":
            if self.fail_next_call and request.headers.get("Mcp-Session-Id") == self.session_id:
                self.fail_next_call = False
                return web.Response(status=404)
            text, content_type = _envelope(body["id"], as_sse=self.sse)
            return web.Response(text=text, content_type=content_type)
        return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601}})


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        PLANNER_ENABLED=True,
        PLANNER_SUPABASE_URL="https://example.supabase.co",
        PLANNER_OAUTH_CLIENT_ID="cid",
        PLANNER_OAUTH_REDIRECT_URI="https://anchor.example/planner/oauth/callback",
    )


async def _seed_credential(sessionmaker, clock) -> None:
    async with sessionmaker() as session:
        session.add(
            PlannerCredential(
                id=1,
                access_token=TOKEN,
                refresh_token="rt",
                expires_at=clock.now_utc() + datetime.timedelta(hours=1),
                status="active",
            )
        )
        await session.commit()


async def test_get_agenda_parses_a_json_body(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock)
    fake = _FakeMcpServer()
    app = web.Application()
    app.router.add_post("/mcp", fake.handle)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("/mcp"))
        planner_client = PlannerClient(client.session, url)
        async with sessionmaker() as session:
            result = await planner_client.get_agenda(
                _settings(), session, clock, date="2026-09-23", timezone="Europe/Paris"
            )
    assert result == {"events": [], "tasks": []}
    assert [c["method"] for c in fake.calls][:2] == ["initialize", "notifications/initialized"]


async def test_get_agenda_parses_a_single_sse_frame(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock)
    fake = _FakeMcpServer(sse=True)
    app = web.Application()
    app.router.add_post("/mcp", fake.handle)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("/mcp"))
        planner_client = PlannerClient(client.session, url)
        async with sessionmaker() as session:
            result = await planner_client.get_agenda(
                _settings(), session, clock, date="2026-09-23", timezone="Europe/Paris"
            )
    assert result == {"events": [], "tasks": []}


async def test_a_disallowed_tool_is_refused_before_any_http_call(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock)
    fake = _FakeMcpServer()
    app = web.Application()
    app.router.add_post("/mcp", fake.handle)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("/mcp"))
        planner_client = PlannerClient(client.session, url)
        async with sessionmaker() as session:
            with pytest.raises(PlannerToolError):
                await planner_client.call_tool(_settings(), session, clock, "update_event", {})
    assert fake.calls == []
    assert "update_event" not in ALLOWED_TOOLS
    assert "delete_event" not in ALLOWED_TOOLS


async def test_a_lost_session_is_re_initialized_exactly_once(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock)
    fake = _FakeMcpServer(fail_next_call=True)
    app = web.Application()
    app.router.add_post("/mcp", fake.handle)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("/mcp"))
        planner_client = PlannerClient(client.session, url)
        async with sessionmaker() as session:
            result = await planner_client.get_agenda(
                _settings(), session, clock, date="2026-09-23", timezone="Europe/Paris"
            )
    assert result == {"events": [], "tasks": []}
    assert [c["method"] for c in fake.calls].count("initialize") == 2


async def test_a_timeout_raises_planner_unavailable(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock)

    class _TimeoutSession:
        def post(self, *args, **kwargs):
            raise asyncio.TimeoutError()

    planner_client = PlannerClient(_TimeoutSession(), "https://example.invalid/mcp")
    async with sessionmaker() as session:
        with pytest.raises(PlannerUnavailable):
            await planner_client.get_agenda(
                _settings(), session, clock, date="2026-09-23", timezone="Europe/Paris"
            )


# --- P3: the write wrapper methods send the right tool + arguments ---------


async def test_create_task_sends_the_right_tool_and_arguments(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock)
    fake = _FakeMcpServer()
    app = web.Application()
    app.router.add_post("/mcp", fake.handle)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("/mcp"))
        planner_client = PlannerClient(client.session, url)
        async with sessionmaker() as session:
            await planner_client.create_task(
                _settings(), session, clock,
                title="Купить молоко", due_date="2026-09-24", client_request_id="anchor:1",
            )
    call = next(c for c in fake.calls if c.get("method") == "tools/call")
    assert call["params"]["name"] == "create_task"
    assert call["params"]["arguments"] == {
        "title": "Купить молоко", "dueDate": "2026-09-24", "clientRequestId": "anchor:1",
    }


async def test_create_event_sends_the_right_tool_and_arguments(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock)
    fake = _FakeMcpServer()
    app = web.Application()
    app.router.add_post("/mcp", fake.handle)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("/mcp"))
        planner_client = PlannerClient(client.session, url)
        async with sessionmaker() as session:
            await planner_client.create_event(
                _settings(), session, clock,
                title="Встреча", start="2026-09-24T18:00:00+02:00", end="2026-09-24T19:00:00+02:00",
                all_day=False, is_private=True, client_request_id="anchor:2",
                timezone="Europe/Berlin",
            )
    call = next(c for c in fake.calls if c.get("method") == "tools/call")
    assert call["params"]["name"] == "create_event"
    assert call["params"]["arguments"] == {
        "title": "Встреча",
        "start": "2026-09-24T18:00:00+02:00",
        "end": "2026-09-24T19:00:00+02:00",
        "allDay": False,
        "isPrivate": True,
        "clientRequestId": "anchor:2",
        "timeZone": "Europe/Berlin",
    }


async def test_complete_task_sends_the_right_tool_and_arguments(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0)
    await _seed_credential(sessionmaker, clock)
    fake = _FakeMcpServer()
    app = web.Application()
    app.router.add_post("/mcp", fake.handle)
    async with TestClient(TestServer(app)) as client:
        url = str(client.make_url("/mcp"))
        planner_client = PlannerClient(client.session, url)
        async with sessionmaker() as session:
            await planner_client.complete_task(_settings(), session, clock, task_id="abc-123")
    call = next(c for c in fake.calls if c.get("method") == "tools/call")
    assert call["params"]["name"] == "complete_task"
    assert call["params"]["arguments"] == {"id": "abc-123"}


async def test_the_write_tools_are_all_allowlisted() -> None:
    assert {"create_task", "create_event", "complete_task"} <= ALLOWED_TOOLS
