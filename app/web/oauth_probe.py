"""A dry-run OAuth front for claude.ai: it records shapes and grants nothing.

The connector plan (anchor-claude-connector-plan.md section 11) says no
OAuth code is written until we know what claude.ai actually sends: which
registration method it picks, its CIMD `client_id` or DCR body, its
callback, and whether and how it sends `resource`. Nothing but a public
server that answers OAuth discovery can observe that. This is that
server, and nothing more:

- Off unless `CLAUDE_OAUTH_PROBE` is `both`, `cimd` or `dcr` (which
  registration method the metadata advertises). Off, no route exists.
- **It never issues anything.** `/mcp/claude` is always 401; authorize
  shows a page and never redirects, so no code, no token, no
  connection. Registration returns one fixed public `client_id` and
  stores nothing. It writes no row, sends no Telegram message, reads no
  content, and imports nothing that could (tests/test_oauth_probe.py).
- **It logs shapes, never values:** the route, parameter and header
  names, and comparisons against what C2 will require (is the callback
  the expected one; is `resource` exact, differently cased, slashed or
  other). A `client_id` URL is logged as its host, plus its path only
  when the host is claude.ai or claude.com, since that document is
  Anthropic's public metadata and C2 must pin it (docs/decisions.md,
  "C2 dry run -- the probe"). `state`, challenges, codes, tokens,
  header values, cookies and addresses are never logged.

C2 replaces this module with the real authorization server
(app/web/oauth.py). Until then, the dry-run checklist is
docs/claude-connector-dry-run.md.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse

from aiohttp import web

from app.config import Settings

logger = logging.getLogger(__name__)

MODES = ("both", "cimd", "dcr")
SCOPE = "anchor.read"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PUBLIC_CLIENT_HOSTS = ("claude.ai", "claude.com")
DRY_RUN_CLIENT_ID = "anchor-dry-run"
MAX_BODY = 16 * 1024
MAX_NAMES = 40

MCP_PATH = "/mcp/claude"
PRM_PATH = "/.well-known/oauth-protected-resource"
ASM_PATH = "/.well-known/oauth-authorization-server"

WAITING_PAGE = (
    "<!doctype html><html lang=ru><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>Anchor</title>"
    "<p>Проверка подключения: запрос получен. Подключение пока не работает.</p>"
    "<p>Можно закрыть эту страницу.</p></html>"
)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}

_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")
_WELL_KNOWN_RE = re.compile(r"^/\.well-known/[a-z0-9._/-]{1,100}$")

GRANT_TYPES = ("authorization_code", "refresh_token", "client_credentials")
AUTH_METHODS = ("none", "client_secret_basic", "client_secret_post", "private_key_jwt")


def issuer(settings: Settings) -> str:
    return settings.PUBLIC_URL.rstrip("/")


def resource(settings: Settings) -> str:
    return issuer(settings) + MCP_PATH


def challenge(settings: Settings) -> str:
    return f'Bearer resource_metadata="{issuer(settings)}{PRM_PATH}{MCP_PATH}", scope="{SCOPE}"'


def compare_resource(value: str | None, expected: str) -> str:
    """How a `resource` parameter relates to the canonical URI.

    `exact`, `case` (scheme or host case differs), `slash` (a trailing
    slash), `case_slash`, `other` or `absent`. C2 accepts the first four.
    """
    if value is None:
        return "absent"
    if value == expected:
        return "exact"
    trimmed = value[:-1] if value.endswith("/") else value
    if trimmed == expected:
        return "slash"
    parts = urllib.parse.urlsplit(trimmed)
    lowered = parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower())
    if urllib.parse.urlunsplit(lowered) == expected:
        return "case" if trimmed == value else "case_slash"
    return "other"


def _vocab(value, allowed: tuple[str, ...]) -> str:
    if value is None:
        return "absent"
    return value if value in allowed else "other"


def _names(keys) -> str:
    """Sorted, de-duplicated names; anything odd is counted, not shown."""
    names = sorted(
        {k.lower() for k in keys if isinstance(k, str) and _NAME_RE.match(k)}
    )
    odd = sum(1 for k in keys if not (isinstance(k, str) and _NAME_RE.match(k)))
    out = ",".join(names[:MAX_NAMES])
    if odd or len(names) > MAX_NAMES:
        out += f",+{odd + max(0, len(names) - MAX_NAMES)}"
    return out


def _client_id(value) -> dict:
    if value is None:
        return {"client_id_kind": "absent"}
    if value == DRY_RUN_CLIENT_ID:
        return {"client_id_kind": "dry_run"}
    parts = urllib.parse.urlsplit(str(value))
    if parts.scheme != "https" or not parts.hostname:
        return {"client_id_kind": "opaque"}
    host = parts.hostname.lower()
    out = {
        "client_id_kind": "url",
        "client_host": host if _HOST_RE.match(host) else "invalid",
    }
    if host in PUBLIC_CLIENT_HOSTS:
        path_ok = _PATH_RE.match(parts.path) and not parts.query and not parts.fragment
        out["client_id_path"] = parts.path if path_ok else "unusual"
    return out


def _log(request: web.Request, route: str, fields, **extra) -> None:
    logger.info(
        "oauth probe",
        extra={
            "event": "oauth_probe",
            "route": route,
            "fields": _names(list(fields)),
            "header_names": _names(list(request.headers.keys())),
            **extra,
        },
    )


def _secure(response: web.StreamResponse) -> web.StreamResponse:
    response.headers.update(SECURITY_HEADERS)
    return response


def _json(body: dict, status: int = 200) -> web.StreamResponse:
    return _secure(web.json_response(body, status=status))


async def _body(request: web.Request) -> bytes | None:
    if request.content_length is not None and request.content_length > MAX_BODY:
        return None
    raw = await request.content.read(MAX_BODY + 1)
    return None if len(raw) > MAX_BODY else raw


def _form(raw: bytes) -> dict[str, str]:
    try:
        pairs = urllib.parse.parse_qsl(raw.decode(), keep_blank_values=True)
    except UnicodeDecodeError:
        return {}
    return dict(pairs)


# --- handlers ---


async def protected_resource(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    _log(request, request.match_info.route.resource.canonical, request.query.keys())
    return _json(
        {
            "resource": resource(settings),
            "authorization_servers": [issuer(settings)],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
        }
    )


async def authorization_server(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    mode = settings.CLAUDE_OAUTH_PROBE
    base = issuer(settings)
    _log(request, ASM_PATH, request.query.keys(), probe_mode=mode)
    body = {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "authorization_response_iss_parameter_supported": True,
        "scopes_supported": [SCOPE],
    }
    if mode in ("both", "cimd"):
        body["client_id_metadata_document_supported"] = True
    if mode in ("both", "dcr"):
        body["registration_endpoint"] = f"{base}/oauth/register"
    return _json(body)


async def mcp(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    _log(request, MCP_PATH, request.query.keys(), http_method=request.method)
    response = web.Response(
        status=401, headers={"WWW-Authenticate": challenge(settings)}
    )
    return _secure(response)


async def register(request: web.Request) -> web.StreamResponse:
    raw = await _body(request)
    try:
        body = json.loads(raw) if raw is not None else None
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        _log(request, "/oauth/register", (), outcome="unreadable")
        return _json({"error": "invalid_client_metadata"}, status=400)
    redirect_uris = body.get("redirect_uris")
    grant_types = body.get("grant_types")
    grants = grant_types if isinstance(grant_types, list) else []
    expected = redirect_uris == [CALLBACK]
    _log(
        request,
        "/oauth/register",
        body.keys(),
        redirect_uri_expected=expected,
        grant_type="+".join(sorted({_vocab(g, GRANT_TYPES) for g in grants}))
        or "absent",
        auth_method=_vocab(body.get("token_endpoint_auth_method"), AUTH_METHODS),
    )
    if not expected:
        return _json({"error": "invalid_redirect_uri"}, status=400)
    # A public client: the id is not a credential, and nothing is stored.
    return _json(
        {
            "client_id": DRY_RUN_CLIENT_ID,
            "redirect_uris": [CALLBACK],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status=201,
    )


async def authorize(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    query = request.query
    _log(
        request,
        "/oauth/authorize",
        query.keys(),
        **_client_id(query.get("client_id")),
        redirect_uri_expected=query.get("redirect_uri") == CALLBACK,
        resource_form=compare_resource(query.get("resource"), resource(settings)),
        pkce_method=_vocab(query.get("code_challenge_method"), ("S256", "plain")),
        response_type=_vocab(query.get("response_type"), ("code",)),
        scope_form=_vocab(query.get("scope"), (SCOPE, "")),
    )
    return _secure(web.Response(text=WAITING_PAGE, content_type="text/html"))


async def token(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    raw = await _body(request)
    form = _form(raw) if raw is not None else {}
    _log(
        request,
        "/oauth/token",
        form.keys(),
        **_client_id(form.get("client_id")),
        grant_type=_vocab(form.get("grant_type"), GRANT_TYPES),
        resource_form=compare_resource(form.get("resource"), resource(settings)),
    )
    return _json({"error": "invalid_grant"}, status=400)


async def revoke(request: web.Request) -> web.StreamResponse:
    raw = await _body(request)
    form = _form(raw) if raw is not None else {}
    _log(request, "/oauth/revoke", form.keys(), **_client_id(form.get("client_id")))
    return _secure(web.Response(status=200))


@web.middleware
async def unrouted_well_known(request: web.Request, handler):
    """Log a discovery path claude.ai tried that the probe does not serve.

    Only `/.well-known/...` paths of a plain shape are named; they carry
    no secret. Any other unrouted path (it could be a capability URL)
    passes through unlogged.
    """
    try:
        return await handler(request)
    except web.HTTPNotFound:
        if _WELL_KNOWN_RE.match(request.path):
            logger.info(
                "oauth probe",
                extra={
                    "event": "oauth_probe",
                    "route": "unrouted",
                    "fields": request.path,
                },
            )
        raise


def register_routes(app: web.Application, settings: Settings) -> None:
    """Mount the probe. Must run before Grok's `/mcp/{token}` route, which
    would otherwise match `/mcp/claude` first (and 404 it)."""
    app.middlewares.append(unrouted_well_known)
    for path in (PRM_PATH, PRM_PATH + MCP_PATH):
        app.router.add_get(path, protected_resource)
    app.router.add_get(ASM_PATH, authorization_server)
    app.router.add_route("*", MCP_PATH, mcp)
    app.router.add_post("/oauth/register", register)
    app.router.add_get("/oauth/authorize", authorize)
    app.router.add_post("/oauth/token", token)
    app.router.add_post("/oauth/revoke", revoke)
