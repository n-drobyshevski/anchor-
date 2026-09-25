"""The Claude connector's authorization server: HTTP only.

anchor-claude-connector-plan.md sections 4 and 5, shaped by the dry run
(docs/decisions.md, "C2 -- the dry run's answers"). Registered only
when CLAUDE_ACCESS_ENABLED is on; off, every path here is aiohttp's own
404. State lives in app/web/oauth_store.py; this module validates,
answers, and never writes a row itself.

- **Metadata.** RFC 9728 at the path-suffixed URL claude.ai asked for
  (and at the root), RFC 8414 advertising CIMD only, `S256` only, and
  `authorization_response_iss_parameter_supported`. `issuer` equals
  `authorization_servers[0]` byte for byte.
- **Authorize** validates everything before any state exists, and a
  failure is a plain page, never a redirect (OAuth 2.1's rule for an
  untrusted redirect). Success is a pending request in memory and a
  waiting page with the confirmation code the user types into Telegram.
  **Nothing on this path sends a Telegram message**: the code travels
  from the screen to Telegram by hand.
- **Status** answers only the browser that holds the binding cookie.
  Without it -- or with another request's, or for an unknown handle --
  the answer is one identical page, so a guessed handle yields nothing.
  The page polls with `<meta refresh>`: the CSP allows no script.
- **Token** and **revoke** are form posts from claude.ai's servers.

Every response carries `Cache-Control: no-store`, `Referrer-Policy:
no-referrer` and a CSP with `frame-ancestors 'none'`. aiohttp's access
log is off (app/main.py), and nothing here logs a code, token, state,
handle, cookie or client id: routes, reasons and ids only.
"""

from __future__ import annotations

import html
import logging
import re
import urllib.parse

from aiohttp import web

from app.config import Settings
from app.web import oauth_store

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp/claude"
PRM_PATH = "/.well-known/oauth-protected-resource"
ASM_PATH = "/.well-known/oauth-authorization-server"
AUTHORIZE_PATH = "/oauth/authorize"
STATUS_PATH = "/oauth/authorize/status"
TOKEN_PATH = "/oauth/token"
REVOKE_PATH = "/oauth/revoke"

COOKIE = "__Host-anchor_oauth"
POLL_SECONDS = 3
MAX_BODY = 16 * 1024
MAX_STATE = 512

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
}

_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")

TITLE = "Anchor · подключение Claude"
WAITING_TEXT = (
    "Открой Telegram и отправь боту:<br><b>/claude connect {code}</b><br><br>"
    "Если ты не подключал Claude сам — просто закрой эту страницу."
)
UNKNOWN_CLIENT = "Клиент не распознан."
BAD_REQUEST = "Запрос на подключение некорректен. Начни подключение в claude.ai заново."
BUSY = "Сейчас нельзя начать подключение. Попробуй позже."
EXPIRED = "Запрос не найден или устарел. Начни подключение в claude.ai заново."


def issuer(settings: Settings) -> str:
    return settings.PUBLIC_URL.rstrip("/")


def resource(settings: Settings) -> str:
    return issuer(settings) + MCP_PATH


def challenge(settings: Settings) -> str:
    return (
        f'Bearer resource_metadata="{issuer(settings)}{PRM_PATH}{MCP_PATH}", '
        f'scope="{oauth_store.SCOPE}"'
    )


def normalise_resource(value: str) -> str:
    """Lowercase the scheme and host and drop one trailing slash (plan 5.2)."""
    parts = urllib.parse.urlsplit(value)
    trimmed = parts._replace(scheme=parts.scheme.lower(), netloc=parts.netloc.lower())
    out = urllib.parse.urlunsplit(trimmed)
    return out[:-1] if out.endswith("/") else out


def _log(route: str, outcome: str) -> None:
    logger.info("oauth", extra={"event": "oauth", "route": route, "reason": outcome})


def _secure(response: web.StreamResponse) -> web.StreamResponse:
    response.headers.update(SECURITY_HEADERS)
    return response


def _json(body: dict, status: int = 200) -> web.StreamResponse:
    return _secure(web.json_response(body, status=status))


def _page(body_html: str, status: int = 200, refresh: str | None = None) -> web.StreamResponse:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    document = (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"{meta}<title>{TITLE}</title>"
        "<style>body{font:18px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:34rem;"
        "padding:0 1rem}b{font-size:1.3em;letter-spacing:.05em}</style></head>"
        f"<body><p>{body_html}</p></body></html>"
    )
    return _secure(web.Response(text=document, status=status, content_type="text/html"))


def _expired() -> web.StreamResponse:
    return _page(html.escape(EXPIRED), status=404)


def _address(request: web.Request) -> str:
    """The client address for the per-address cap.

    Railway's edge appends the real peer to X-Forwarded-For, so the last
    entry is the one a client cannot forge; anything earlier can be.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()[:64]
    return (request.remote or "")[:64]


async def _form(request: web.Request) -> dict[str, str] | None:
    if request.content_length is not None and request.content_length > MAX_BODY:
        return None
    raw = await request.content.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        return None
    try:
        pairs = urllib.parse.parse_qsl(raw.decode(), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError:
        return None
    form: dict[str, str] = {}
    for key, value in pairs:
        if key in form:  # a repeated parameter is malformed (RFC 6749 3.1)
            return None
        form[key] = value
    return form


# --- metadata ---


async def protected_resource(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    return _json(
        {
            "resource": resource(settings),
            "authorization_servers": [issuer(settings)],
            "scopes_supported": [oauth_store.SCOPE],
            "bearer_methods_supported": ["header"],
        }
    )


async def authorization_server(request: web.Request) -> web.StreamResponse:
    base = issuer(request.app["settings"])
    return _json(
        {
            "issuer": base,
            "authorization_endpoint": base + AUTHORIZE_PATH,
            "token_endpoint": base + TOKEN_PATH,
            "revocation_endpoint": base + REVOKE_PATH,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
            "scopes_supported": [oauth_store.SCOPE],
            "client_id_metadata_document_supported": True,
        }
    )


# --- authorize ---


def validate_authorize(query, settings: Settings) -> tuple[str | None, str]:
    """(None, scope) if the request may proceed, else (reason, page text)."""
    if query.get("client_id") != oauth_store.CLIENT_ID:
        return "client", UNKNOWN_CLIENT
    if query.get("redirect_uri") != oauth_store.REDIRECT_URI:
        return "redirect_uri", BAD_REQUEST
    if query.get("response_type") != "code":
        return "response_type", BAD_REQUEST
    if query.get("code_challenge_method") != "S256" or not _CHALLENGE_RE.match(
        query.get("code_challenge", "")
    ):
        return "pkce", BAD_REQUEST
    requested = query.get("resource")
    if requested is None or normalise_resource(requested) != resource(settings):
        return "resource", BAD_REQUEST
    scope = query.get("scope", "").split()
    if any(item != oauth_store.SCOPE for item in scope):
        return "scope", BAD_REQUEST
    state = query.get("state", "")
    if not state or len(state) > MAX_STATE:
        return "state", BAD_REQUEST
    for key in query:
        if len(query.getall(key)) > 1:
            return "repeated", BAD_REQUEST
    return None, oauth_store.SCOPE


def _waiting(handle: str, code: str) -> web.StreamResponse:
    url = f"{STATUS_PATH}?h={handle}"
    return _page(
        WAITING_TEXT.format(code=html.escape(code)), refresh=f"{POLL_SECONDS};url={url}"
    )


async def authorize(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    reason, scope_or_text = validate_authorize(request.query, settings)
    if reason is not None:
        _log(AUTHORIZE_PATH, f"refused_{reason}")
        return _page(html.escape(scope_or_text), status=400)

    store: oauth_store.PendingStore = request.app["claude_pending"]
    browser_secret = request.cookies.get(COOKIE, "")
    fresh_cookie = not _SECRET_RE.match(browser_secret)
    if fresh_cookie:
        browser_secret = oauth_store.new_secret()
    created = store.create(
        address=_address(request),
        browser_secret=browser_secret,
        client_id=oauth_store.CLIENT_ID,
        redirect_uri=oauth_store.REDIRECT_URI,
        code_challenge=request.query["code_challenge"],
        resource=resource(settings),
        scope=scope_or_text,
        state=request.query["state"],
    )
    if created is None:
        _log(AUTHORIZE_PATH, "busy")
        return _page(html.escape(BUSY), status=429)
    handle, entry = created
    _log(AUTHORIZE_PATH, "pending")
    response = _waiting(handle, entry.code)
    if fresh_cookie:
        response.set_cookie(
            COOKIE, browser_secret, secure=True, httponly=True, samesite="Lax", path="/"
        )
    return response


async def status(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    handle = request.query.get("h", "")
    store: oauth_store.PendingStore = request.app["claude_pending"]
    entry = (
        store.for_browser(handle, request.cookies.get(COOKIE))
        if _HANDLE_RE.match(handle)
        else None
    )
    if entry is None:
        return _expired()
    if entry.auth_code is None:
        return _waiting(handle, entry.code)
    query = urllib.parse.urlencode(
        {"code": entry.auth_code, "state": entry.state, "iss": issuer(settings)}
    )
    store.drop(entry)
    _log(STATUS_PATH, "redirect")
    return _secure(web.Response(status=302, headers={"Location": f"{entry.redirect_uri}?{query}"}))


# --- token and revoke ---


def _grant_error(error: str = "invalid_grant", status: int = 400) -> web.StreamResponse:
    return _json({"error": error}, status=status)


async def token(request: web.Request) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    form = await _form(request)
    if form is None:
        _log(TOKEN_PATH, "malformed")
        return _grant_error("invalid_request")
    expected = resource(settings)

    def resource_ok(bound: str) -> bool:
        # Absent is fine (the code or token is already bound); present
        # must name this resource.
        sent = form.get("resource")
        return bound == expected and (sent is None or normalise_resource(sent) == expected)

    grant_type = form.get("grant_type")
    async with request.app["sessionmaker"]() as session:
        if grant_type == "authorization_code":
            if (
                form.get("client_id") != oauth_store.CLIENT_ID
                or not form.get("code")
                or not _VERIFIER_RE.match(form.get("code_verifier", ""))
            ):
                _log(TOKEN_PATH, "refused_code_request")
                return _grant_error()
            issued = await oauth_store.redeem_code(
                session,
                request.app["clock"],
                code=form["code"],
                client_id=form["client_id"],
                redirect_uri=form.get("redirect_uri", ""),
                verifier=form["code_verifier"],
                resource_ok=resource_ok,
            )
        elif grant_type == "refresh_token":
            client_id = form.get("client_id")
            if not form.get("refresh_token") or (
                client_id is not None and client_id != oauth_store.CLIENT_ID
            ):
                _log(TOKEN_PATH, "refused_refresh_request")
                return _grant_error()
            issued = await oauth_store.rotate_refresh(
                session,
                request.app["clock"],
                refresh_token=form["refresh_token"],
                client_id=client_id,
                resource_ok=resource_ok,
            )
        else:
            _log(TOKEN_PATH, "unsupported_grant_type")
            return _grant_error("unsupported_grant_type")
    if issued is None:
        _log(TOKEN_PATH, f"refused_{grant_type}")
        return _grant_error()
    _log(TOKEN_PATH, f"issued_{grant_type}")
    return _json(
        {
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "refresh_token": issued.refresh_token,
            "scope": oauth_store.SCOPE,
        }
    )


async def revoke(request: web.Request) -> web.StreamResponse:
    form = await _form(request)
    if form is None or not form.get("client_id") or "token" not in form:
        _log(REVOKE_PATH, "malformed")
        return _grant_error("invalid_request")
    async with request.app["sessionmaker"]() as session:
        await oauth_store.revoke_token(
            session, request.app["clock"], token=form["token"], client_id=form["client_id"]
        )
    _log(REVOKE_PATH, "done")
    return _secure(web.Response(status=200))


def register(app: web.Application, settings: Settings, pending: oauth_store.PendingStore) -> None:
    """Mount the authorization server. app/main.py calls this only with
    CLAUDE_ACCESS_ENABLED on, and before Grok's /mcp/{token} route."""
    app["claude_pending"] = pending
    for path in (PRM_PATH, PRM_PATH + MCP_PATH):
        app.router.add_get(path, protected_resource)
    app.router.add_get(ASM_PATH, authorization_server)
    app.router.add_get(AUTHORIZE_PATH, authorize)
    app.router.add_get(STATUS_PATH, status)
    app.router.add_post(TOKEN_PATH, token)
    app.router.add_post(REVOKE_PATH, revoke)

