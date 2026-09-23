"""Security headers, CSRF (fetch-metadata + Origin), and bounded JSON
reading for the web chat (web-chat plan track 2, design section 5).

One aiohttp middleware, `build_middleware(settings)`, does two jobs:

1. **On every response to `/`, `/static/*` and `/api/*`** (never
   `/telegram/webhook`, `/healthz` or `/readyz` -- those keep behaving
   exactly as they did before this module existed): stamp the fixed set
   of security headers, plus a path-appropriate `Cache-Control`.

2. **Before the handler runs, for every `/api/*` request**: the
   fetch-metadata + Origin CSRF check, and (for a mutating method) the
   `Content-Type: application/json` check. A rejection here never calls
   the handler at all.

The `Content-Security-Policy`'s `require-trusted-types-for 'script'` is
doing real work, not just documentation: combined with `script-src
'self'` and no `unsafe-inline`, it makes an accidental `innerHTML`
assignment in app.js throw at runtime rather than silently render
untrusted bot output as HTML (app/web/static/app.js's own module
docstring states the same rule from the other side; tests/
test_web_static.py greps for it structurally).

**Why the SSE handler (`GET /api/events`, app/web/routes.py) cannot use
this middleware for its own headers.** `aiohttp.web.StreamResponse`
flushes its status line and headers to the socket the moment
`await resp.prepare(request)` runs, which happens *inside* that
handler, before control ever returns to this middleware. Headers set
here, after `handler(request)` returns, are true no-ops on an
already-prepared stream -- not an error, just bytes that never reach
the client. `build_headers()` is exported precisely so routes.py can
merge the same header set into the SSE response's `headers=` argument
before calling `prepare()`, rather than duplicating the list.

**Why the CSRF check lives here and not in each handler.** The design's
own adversarial review (finding 3) flagged that a *missing*
`Sec-Fetch-Site` header must fail closed, not fail open by only
matching against explicitly disallowed values -- a bespoke check
copy-pasted into eight handlers is exactly the kind of surface where
one copy quietly drifts. One function, one place, applied uniformly to
every `/api/*` path before any handler-specific logic runs.
"""

from __future__ import annotations

import json
import urllib.parse

from aiohttp import web

from app.config import Settings

# --- headers --------------------------------------------------------------

# Exact string per the web-chat plan's HTTP API contract. `default-src
# 'none'` means every fetch directive below is a deliberate, individual
# opt-in, not a relaxation of a permissive baseline.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; manifest-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "require-trusted-types-for 'script'; trusted-types 'none'"
)

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
HSTS_VALUE = "max-age=63072000; includeSubDomains"


def _is_https(public_url: str) -> bool:
    return urllib.parse.urlparse(public_url.strip()).scheme == "https"


def build_headers(settings: Settings) -> dict[str, str]:
    """The path-independent security headers. HSTS only over https --
    sending it to an http://localhost dev deploy would tell the browser
    to *require* https for this host from then on, which is exactly
    wrong for a box that is not serving it.
    """
    headers = {
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": PERMISSIONS_POLICY,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "X-Robots-Tag": "noindex, nofollow",
    }
    if _is_https(settings.PUBLIC_URL):
        headers["Strict-Transport-Security"] = HSTS_VALUE
    return headers


def _cache_control_for(path: str) -> str | None:
    if path == "/" or path.startswith("/api/"):
        return "no-store"
    if path.startswith("/static/"):
        return "no-cache"
    return None


def _is_web_path(path: str) -> bool:
    return path == "/" or path.startswith("/static/") or path.startswith("/api/")


def _apply_headers(response: web.StreamResponse, settings: Settings, path: str) -> None:
    for name, value in build_headers(settings).items():
        response.headers[name] = value
    cache_control = _cache_control_for(path)
    if cache_control is not None:
        response.headers["Cache-Control"] = cache_control


# --- CSRF: fetch metadata + Origin ----------------------------------------

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_of(public_url: str) -> str:
    return public_url.strip().rstrip("/")


def check_request_origin(request: web.Request, settings: Settings) -> bool:
    """True iff this `/api/*` request may proceed; False means "403".

    Fails closed on a **missing** `Sec-Fetch-Site` header, not only on
    an explicit `cross-site`/`same-site` value (the second adversarial
    critique's finding 3, by name): a browser old enough, or a proxy
    aggressive enough, to strip fetch-metadata headers gets treated
    exactly like a cross-site request, not waved through on the
    strength of `SameSite=Strict` cookies alone.

    `same-origin` is what every same-page `fetch()`/`EventSource` call
    this app's own app.js makes will carry. `none` (a top-level
    navigation with no referring page, e.g. a typed URL or a bookmark)
    is accepted only for a non-mutating request -- there is no
    legitimate top-level navigation to a POST endpoint.

    `Origin`, when the browser sends it, must equal `PUBLIC_URL`'s own
    origin exactly. A mutating request that sends no `Origin` at all is
    rejected outright (some legitimate same-origin requests omit it,
    but a state-changing one should not rely on that); a GET may omit
    it, matching ordinary same-origin GET fetches, which browsers often
    do not attach an Origin header to at all.
    """
    site = request.headers.get("Sec-Fetch-Site")
    if site not in ("same-origin", "none"):
        return False
    mutating = request.method in _MUTATING_METHODS
    if site == "none" and mutating:
        return False
    origin = request.headers.get("Origin")
    if origin is not None:
        return origin == _origin_of(settings.PUBLIC_URL)
    return not mutating


def _content_type_ok(request: web.Request) -> bool:
    content_type = request.headers.get("Content-Type", "")
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def _json_error(status: int, error: str) -> web.Response:
    return web.json_response({"error": error}, status=status)


async def _check_api_request(request: web.Request, settings: Settings) -> web.Response | None:
    """Run for every `/api/*` request, before its handler. Returns an
    early-rejection Response, or None to let the handler run.
    """
    if not check_request_origin(request, settings):
        return _json_error(403, "forbidden")
    if request.method in _MUTATING_METHODS and not _content_type_ok(request):
        return _json_error(415, "unsupported_media_type")
    return None


# --- the middleware ---------------------------------------------------------


def build_middleware(settings: Settings):
    """One aiohttp middleware doing both jobs described in the module
    docstring. A factory (not a bare `@web.middleware` function) because
    it closes over `settings`, matching this codebase's style of
    passing dependencies in explicitly rather than through aiohttp's
    app-level DI (app/tg/router.py's `build_router` does the same).
    """

    @web.middleware
    async def _security_middleware(
        request: web.Request, handler
    ) -> web.StreamResponse:
        path = request.path
        if not _is_web_path(path):
            return await handler(request)

        if path.startswith("/api/"):
            rejection = await _check_api_request(request, settings)
            if rejection is not None:
                _apply_headers(rejection, settings, path)
                return rejection

        response = await handler(request)
        _apply_headers(response, settings, path)
        return response

    return _security_middleware


# --- bounded JSON reading ---------------------------------------------------

MAX_BODY_BYTES = 16 * 1024


class LengthRequired(Exception):
    """No Content-Length header on a request that must have a bounded body."""


class PayloadTooLarge(Exception):
    """Content-Length, or the body actually read, exceeds MAX_BODY_BYTES."""


class BadJson(Exception):
    """The body was within limits but not valid JSON (or not a JSON object)."""


async def read_json_bounded(request: web.Request, max_bytes: int = MAX_BODY_BYTES) -> dict:
    """Read and parse a request body no larger than `max_bytes`.

    Two independent checks, not one: `Content-Length` is trusted for a
    cheap up-front rejection (411/413), but the actual read is also
    capped at `max_bytes + 1` so a client that lies about its own
    Content-Length (or sends chunked/no length at all past whatever the
    header claimed) still cannot hand this process an unbounded body to
    buffer.
    """
    content_length = request.content_length
    if content_length is None:
        raise LengthRequired()
    if content_length > max_bytes:
        raise PayloadTooLarge()
    body = await request.content.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise PayloadTooLarge()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadJson() from exc
    if not isinstance(data, dict):
        raise BadJson()
    return data
