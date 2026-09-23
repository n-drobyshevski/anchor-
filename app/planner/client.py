"""A small hand-written MCP client: JSON-RPC 2.0 over aiohttp.

Not the `mcp` Python SDK. The SDK depends on `httpx`, and this repo's
lockfile carries `httpx2` (a transitive dependency of `openai`) with no
plain `httpx` anywhere in it -- adding the SDK would pull in a second,
overlapping HTTP stack for one small client. `aiohttp` is already
present (aiogram depends on it), so this file uses that instead. See
the design review for the verification (`uv.lock` has no `httpx`).

Speaks streamable-HTTP MCP against the planner's `/api/mcp` endpoint:
POST JSON-RPC bodies, `Authorization: Bearer <access token>`, `Accept:
application/json, text/event-stream`, and once a session exists,
`Mcp-Session-Id` plus `MCP-Protocol-Version` on every request after
`initialize`. `protocolVersion: "2025-06-18"` in the `initialize` call,
which the planner's `@modelcontextprotocol/sdk@1.29.0` accepts (see the
design review's P0 spike).

`ALLOWED_TOOLS` is enforced here, not only by the planner's own RLS: a
future caller that tries `update_event` (which can edit the partner's
joint events) or any `delete_*` tool fails fast and loud, in Anchor's
own process, rather than depending on the planner never adding a tool
this client should not use.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.planner.auth import get_access_token

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "anchor", "version": "1.0"}

# 5s: this client sits on the PLANNER_SYNC job path (and, later, the
# write path), never on the chat-turn path itself -- a chat turn only
# ever reads the local snapshot (see app/planner/snapshot.py). A slow
# planner therefore costs one job retry, not a delayed reply.
_TIMEOUT = aiohttp.ClientTimeout(total=5)

# Plan section 2.3: no update or delete tool. update_event can edit the
# partner's own joint events (design review, table 1), and nothing here
# needs to remove anything.
ALLOWED_TOOLS: frozenset[str] = frozenset(
    {"get_agenda", "list_tasks", "get_workspace", "create_task", "create_event", "complete_task"}
)


class PlannerUnavailable(Exception):
    """Network failure or timeout talking to the planner."""


class PlannerToolError(Exception):
    """A JSON-RPC error came back, the tool is not allowlisted, or the
    response shape was not what an MCP tool call returns."""


def _parse_body(raw: str) -> dict | None:
    """A JSON-RPC response body, whether sent as plain JSON or as a
    single SSE `data:` frame. None if neither parses."""
    raw = raw.strip()
    if not raw:
        return None
    if raw[0] == "{":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                return None
    return None


class PlannerClient:
    """One client per running process, reused across calls.

    The `aiohttp.ClientSession` is owned by the caller (app/main.py
    builds and closes it, mirroring how app/llm/openrouter.py's shared
    AsyncOpenAI client works) -- this class never opens or closes its
    own connection pool.
    """

    def __init__(self, http: aiohttp.ClientSession, base_url: str) -> None:
        self._http = http
        self._url = base_url
        self._session_id: str | None = None
        self._initialized = False
        self._next_id = 1
        self._init_lock = asyncio.Lock()

    def _id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _headers(self, token: str, *, with_session: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if with_session and self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return headers

    async def _post(
        self, body: dict, *, token: str, with_session: bool = True
    ) -> tuple[int, dict | None, str | None]:
        headers = self._headers(token, with_session=with_session)
        try:
            async with self._http.post(
                self._url, json=body, headers=headers, timeout=_TIMEOUT
            ) as resp:
                session_id = resp.headers.get("Mcp-Session-Id")
                raw = await resp.text()
                return resp.status, _parse_body(raw), session_id
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise PlannerUnavailable(type(exc).__name__) from exc

    async def _do_initialize(self, token: str) -> None:
        status, payload, session_id = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            },
            token=token,
            with_session=False,
        )
        if status >= 400 or payload is None or "result" not in payload:
            raise PlannerUnavailable(f"initialize failed: status {status}")
        self._session_id = session_id
        # Notification: no "id", no response expected.
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token=token)
        self._initialized = True

    async def _ensure_initialized(self, token: str) -> None:
        async with self._init_lock:
            if not self._initialized:
                await self._do_initialize(token)

    async def call_tool(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        name: str,
        arguments: dict[str, Any],
    ) -> dict:
        """Call one MCP tool; returns its parsed JSON text content.

        Re-initializes once and retries on a 400/404 (session lost --
        the planner's Redis session store expired or was cycled), per
        the design review's client spec.
        """
        if name not in ALLOWED_TOOLS:
            raise PlannerToolError(f"tool not allowed: {name}")

        token = await get_access_token(session, settings, clock)
        await self._ensure_initialized(token)

        call_body = {
            "jsonrpc": "2.0",
            "id": self._id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        status, payload, _ = await self._post(call_body, token=token)
        if status in (400, 404):
            self._initialized = False
            await self._ensure_initialized(token)
            call_body["id"] = self._id()
            status, payload, _ = await self._post(call_body, token=token)

        if status == 401:
            # Let the caller re-derive a fresh token on the next attempt
            # via get_access_token rather than papering over it here.
            raise PlannerToolError("planner rejected the access token (401)")
        if payload is None:
            raise PlannerUnavailable(f"unparseable response: status {status}")
        if "error" in payload:
            error = payload["error"] or {}
            raise PlannerToolError(f"tool error: code {error.get('code')}")

        result = payload.get("result")
        if not isinstance(result, dict):
            raise PlannerToolError("malformed tool result")
        text = next(
            (item.get("text") for item in result.get("content", []) if item.get("type") == "text"),
            None,
        )
        if text is None:
            raise PlannerToolError("tool result had no text content")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlannerToolError("tool result text was not JSON") from exc

    async def get_agenda(
        self,
        settings: Settings,
        session: AsyncSession,
        clock: Clock,
        *,
        date: str,
        timezone: str,
        days: int = 1,
        partner: str = "shared",
    ) -> dict:
        return await self.call_tool(
            settings,
            session,
            clock,
            "get_agenda",
            {"date": date, "timeZone": timezone, "days": days, "partner": partner},
        )


def build_planner_client(settings: Settings, http: aiohttp.ClientSession) -> PlannerClient:
    """Constructed once in app/main.py, shared across the worker's jobs."""
    return PlannerClient(http, settings.PLANNER_MCP_URL)


__all__ = [
    "PROTOCOL_VERSION",
    "ALLOWED_TOOLS",
    "PlannerClient",
    "PlannerUnavailable",
    "PlannerToolError",
    "build_planner_client",
]
