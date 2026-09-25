"""Grok's read-only MCP endpoint: the capability URL (docs/grok-access.md).

`POST /mcp/{token}` speaks the Model Context Protocol's Streamable HTTP
transport in its simplest, stateless form: one JSON-RPC request per
POST, one `application/json` response. That is enough for a client
such as grok.com's custom connector to `initialize`, `tools/list` and
`tools/call`; there are no server-initiated messages, so there is no
SSE stream and GET is 405.

This module is only the authentication. Everything after it -- the
dispatcher, the tools, the limiter, the read notice -- is shared with
the Claude connector in app/web/mcp_core.py, so both clients read the
same data the same way.

The token in the path *is* the credential (a capability URL). Every
refusal -- feature off, malformed, unknown, expired or revoked token --
is the same bare 404, so the endpoint does not say which one it was.

Privacy rules, on top of app/log.py's:
- the token and the path are never logged; aiohttp's access log, which
  would print the path, is disabled in app/main.py;
- log lines carry the grant id, the tool name and a row count only;
- no exception text or traceback is logged or returned.

Every successful tools/call is counted on the grant, and the user gets
a Telegram message on the first read and then at most every
grants.NOTIFY_EVERY, so access is never silent.
"""

from __future__ import annotations

import re

from aiohttp import web

from app.config import Settings
from app.core import grants
from app.web import mcp_core

PATH = "/mcp/{token}"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")

NOTIFY_TEXT = "Grok прочитал: {what}. Отозвать доступ: /revoke"


def _not_found() -> web.Response:
    # Byte-for-byte aiohttp's own 404 for an unrouted path, so a refused
    # token looks exactly like the feature not existing at all.
    return web.HTTPNotFound()


async def handle(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    if not settings.GROK_ACCESS_ENABLED:
        return _not_found()
    token = request.match_info.get("token", "")
    if not _TOKEN_RE.match(token):
        return _not_found()

    async with request.app["sessionmaker"]() as session:
        grant = await grants.find_active_grant(session, request.app["clock"], token)
    if grant is None:
        return _not_found()

    reader = mcp_core.Reader(
        listed=tuple(grant.scopes),
        grant=grant,
        limit_key=grant.id,
        notice=NOTIFY_TEXT,
    )
    return await mcp_core.serve(request, reader, request.app["mcp_limiter"])


def register(app: web.Application, settings: Settings) -> None:
    app["mcp_limiter"] = mcp_core.RateLimiter(settings.GROK_MAX_CALLS_PER_MINUTE)
    app.router.add_route("*", PATH, handle)
