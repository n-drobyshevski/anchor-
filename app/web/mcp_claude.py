"""Claude's read-only MCP endpoint: bearer tokens and windows (C2).

`POST /mcp/claude` is the same stateless JSON-RPC server as Grok's
(app/web/mcp_core.py); only the gate in front of it differs.

- **Authentication is the OAuth access token** from app/web/oauth.py,
  in the `Authorization: Bearer` header only -- never a URL. A missing,
  malformed, expired, revoked or foreign token, a token for another
  audience, or one whose connection is revoked, past its absolute
  lifetime or not the current one, all get the same 401 with the
  `WWW-Authenticate` challenge that sends claude.ai to the metadata.
  401 is reserved for token problems, as the MCP spec requires.
- **Reading needs a window.** A connected Claude sees every tool it
  may ever use in `tools/list` (clients cache tool lists), but a
  `tools/call` outside an open `/claude` window, or for a scope the
  window leaves out, is a tool result with `isError: true` and
  «Доступ закрыт. Открой его в Telegram: /claude» -- not a 403, which
  would send claude.ai into re-authorisation on every call.
- **Every read is announced** in Telegram: «Claude (подключение #3)
  прочитал: журнал (14)», on a window's first read and then at most
  every grants.NOTIFY_EVERY.
- Its own rate limiter, keyed by connection, apart from Grok's.

Logs carry the connection id, the grant id, the tool and a count.
"""

from __future__ import annotations

import re

from aiohttp import web

from app.config import Settings
from app.core import grants
from app.web import mcp_core, oauth, oauth_store

PATH = oauth.MCP_PATH
_BEARER_RE = re.compile(r"^Bearer ([A-Za-z0-9_-]{43})$")

CLOSED_TEXT = "Доступ закрыт. Открой его в Telegram: /claude"
NOTIFY_TEXT = "Claude (подключение #{connection}) прочитал: {{what}}. Закрыть: /revoke"
INSTRUCTIONS = mcp_core.SERVER_INSTRUCTIONS + (
    " Read only what the user's current question needs; do not browse the rest."
)


def _unauthorized(settings: Settings) -> web.StreamResponse:
    return web.Response(status=401, headers={"WWW-Authenticate": oauth.challenge(settings)})


async def handle(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    if not settings.CLAUDE_ACCESS_ENABLED:
        return web.HTTPNotFound()
    match = _BEARER_RE.match(request.headers.get("Authorization", ""))
    if match is None:
        return _unauthorized(settings)

    clock = request.app["clock"]
    async with request.app["sessionmaker"]() as session:
        connection = await oauth_store.authenticate_bearer(
            session, clock, match.group(1), oauth.resource(settings)
        )
        if connection is None:
            return _unauthorized(settings)
        window = await grants.find_open_window(session, clock, connection.id)

    reader = mcp_core.Reader(
        listed=grants.SCOPES,
        grant=window,
        limit_key=connection.id,
        notice=NOTIFY_TEXT.format(connection=connection.id),
        refusal=mcp_core.Refusal(CLOSED_TEXT),
        instructions=INSTRUCTIONS,
    )
    return await mcp_core.serve(request, reader, request.app["claude_limiter"])


def register(app: web.Application, settings: Settings) -> None:
    app["claude_limiter"] = mcp_core.RateLimiter(settings.CLAUDE_MAX_CALLS_PER_MINUTE)
    app.router.add_route("*", PATH, handle)
