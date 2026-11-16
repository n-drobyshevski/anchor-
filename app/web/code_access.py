"""The code window's HTTP routes (anchor-code-window-plan.md section 4.4, CW1).

Three `POST` routes under `/code/v1`, JSON in and out, registered only
when CODE_ACCESS_ENABLED (app/main.py). Claude Code calls them with
`curl` from a cloud session or the CLI; the bot relays vaultd's answer
(app/vault/client.py's `code_tree` / `code_file`, called from this
module only) and forgets it:

- `/code/v1/status`, body `{}`: `{"open": false}`, or `{"open": true,
  "expires_at", "roots", "files_read", "files_left"}`.
- `/code/v1/tree`, body `{"root": <rel>}` optional: vaultd's tree for
  the window's roots, narrowed to `root` when given (it must lie under
  a window root).
- `/code/v1/file`, body `{"path": <rel>}`: `{"path", "class", "sha256",
  "text"}`.

**What guards them, in order.** A method other than POST, a lockout, a
missing or wrong token: the bare 404, byte for byte aiohttp's own for a
route that does not exist, so nothing tells a stranger the routes are
there (app/web/mcp.py does the same). The token is compared as
`sha256(token)` against CODE_ACCESS_TOKEN_SHA256 with
`hmac.compare_digest`; the bot never holds the token itself. Ten wrong
tokens in an hour lock all three routes for an hour (a header that is
missing altogether is not counted, so a scanner without one cannot
lock the user out). Then 120 requests a minute, global; then the body
(400 if it is not the expected JSON object); then the window: with a
good token and none open, `tree` and `file` answer 403
`{"error": "no_window"}`.

**Confinement both ways.** The bot checks the root or path against the
window's roots by segments (app/vault/code_access.py's `covers`, which
also refuses `Anchor/` in any case) before it asks vaultd, and passes
the roots on so vaultd checks again (plan section 3). Outside the roots
is the same 404 `{"error": "not_found"}` as a missing note. A tree entry
vaultd returns outside what was asked is dropped, not relayed.

**The ledger and the notice.** Each successful `tree` or `file` writes
one `code_read` row (entries or 1, and the bytes answered). The first
of a window sends one Telegram message, «Claude Code начал читать
vault: окно до HH:MM», through the same `bot.send_message` the
connector's read notice uses (app/web/mcp_core.py), and stamps
`notified_at`. `CODE_FILES_PER_WINDOW` file reads, then 429 until the
window closes.

**Logs** carry the route name, the status, the window id and counts.
Never the token, a path, a root or text, and no exception text: a
failure logs its type name or vaultd's error code only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from aiohttp import web

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.state import get_state
from app.vault import code_access
from app.vault import errors as vault_errors
from app.vault.client import CodeTree, VaultClient
from app.vault.errors import VaultError
from app.web.ratelimit import _SlidingWindow

logger = logging.getLogger(__name__)

PREFIX = "/code/v1"
ROUTE_NAMES = ("status", "tree", "file")

MAX_BODY = 4096
WRONG_TOKEN_LIMIT = 10
WRONG_TOKEN_WINDOW_S = 60 * 60
LOCKOUT_S = 60 * 60
REQUESTS_PER_MINUTE = 120

NOTICE_TEXT = "Claude Code начал читать vault: окно до {until}"
LOCKOUT_TEXT = (
    "Claude Code: {n} неверных токенов за час. Окно для vault заблокировано на час."
)

# The key the tests use to hand in a fake vault client.
CLIENT_FACTORY_KEY = "code_vault_client_factory"
GUARD_KEY = "code_access_guard"


class Guard:
    """The routes' in-memory limits (plan section 4.4): ten wrong tokens
    in an hour lock all three routes for an hour, and 120 requests a
    minute, both global, like app/web/ratelimit.py. Times come from the
    injected Clock, passed in per call."""

    def __init__(self) -> None:
        self._wrong = _SlidingWindow(WRONG_TOKEN_LIMIT, WRONG_TOKEN_WINDOW_S)
        self._requests = _SlidingWindow(REQUESTS_PER_MINUTE, 60)
        self._locked_until: float | None = None

    def locked(self, now: float) -> bool:
        if self._locked_until is None:
            return False
        if now >= self._locked_until:
            self._locked_until = None
            return False
        return True

    def record_wrong(self, now: float) -> bool:
        """One wrong token. True when this one starts a lockout."""
        if self._wrong.record(now) >= WRONG_TOKEN_LIMIT:
            self._locked_until = now + LOCKOUT_S
            self._wrong.clear()
            return True
        return False

    def allow(self, now: float) -> float | None:
        """None if this request may go on, else seconds until it may."""
        return self._requests.hit(now)


def token_matches(header: str | None, expected_sha256: str) -> bool | None:
    """None when no Authorization header was sent at all; else whether
    `Bearer <token>` hashes to the configured value."""
    if not header:
        return None
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or not expected_sha256:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, expected_sha256.strip().lower())


def _not_found() -> web.Response:
    # Byte-for-byte aiohttp's own 404 for an unrouted path (app/web/mcp.py).
    return web.HTTPNotFound()


def _error(status: int, code: str, **headers: str) -> web.Response:
    return web.json_response({"error": code}, status=status, headers=headers or None)


def _log(route: str, status: int, **extra) -> None:
    logger.info(
        "code access",
        extra={"event": "code_access", "route": route, "http_status": status, **extra},
    )


async def _body(request: web.Request) -> dict | None:
    """The JSON object sent, `{}` for an empty body, None for anything else."""
    if request.content_length is not None and request.content_length > MAX_BODY:
        return None
    # `content.read(n)` may return one network chunk, not the whole body,
    # so read until the end or one byte past the cap.
    raw = b""
    while len(raw) <= MAX_BODY:
        chunk = await request.content.read(MAX_BODY + 1 - len(raw))
        if not chunk:
            break
        raw += chunk
    if len(raw) > MAX_BODY:
        return None
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _vault_failure(route: str, code: str) -> web.Response:
    """vaultd's refusal, as this route answers it."""
    if code == vault_errors.CONFLICT:
        return _error(409, "settings_unusable")
    if code == vault_errors.NOT_FOUND:
        # The tree never refuses a root (plan section 3): its 404 is
        # vaultd's switch being off. A file's is every read refusal.
        return _error(503, "vault_code_off") if route == "tree" else _error(404, "not_found")
    if code == vault_errors.REFUSED:
        return _error(400, "bad_request")
    return _error(502, "vault_unavailable")


async def _notify(request: web.Request, window: code_access.Window) -> None:
    settings: Settings = request.app["settings"]
    try:
        async with request.app["sessionmaker"]() as session:
            timezone = (await get_state(session)).timezone
        assert window.expires_at is not None  # an open window has one
        until = window.expires_at.astimezone(clock_module.zone(timezone)).strftime("%H:%M")
        await request.app["bot"].send_message(settings.ALLOWED_CHAT_ID, NOTICE_TEXT.format(until=until))
    except Exception as exc:  # noqa: BLE001 - a failed notice must not fail the read
        logger.warning("code notice failed", extra={"event": type(exc).__name__})


async def _alert_lockout(request: web.Request) -> None:
    settings: Settings = request.app["settings"]
    try:
        await request.app["bot"].send_message(
            settings.ALLOWED_CHAT_ID, LOCKOUT_TEXT.format(n=WRONG_TOKEN_LIMIT)
        )
    except Exception as exc:  # noqa: BLE001 - the lockout holds either way
        logger.warning("code lockout notice failed", extra={"event": type(exc).__name__})


def _client(request: web.Request) -> VaultClient:
    factory = request.app.get(CLIENT_FACTORY_KEY) or VaultClient.from_settings
    return factory(request.app["settings"])


async def count_notes(client: VaultClient, roots: list[str]) -> CodeTree:
    """vaultd's tree for `roots`, for `/vault code on`'s card (app/tg/
    vault.py), which shows its length only. Raises VaultError."""
    return await client.code_tree(list(roots))


async def _serve(request: web.Request, route: str) -> web.StreamResponse:
    settings: Settings = request.app["settings"]
    clock: Clock = request.app["clock"]
    guard: Guard = request.app[GUARD_KEY]
    now = clock.now_utc().timestamp()

    if request.method != "POST" or not settings.CODE_ACCESS_ENABLED or guard.locked(now):
        return _not_found()
    matched = token_matches(request.headers.get("Authorization"), settings.CODE_ACCESS_TOKEN_SHA256)
    if not matched:
        if matched is False and guard.record_wrong(now):
            logger.warning("code access locked", extra={"event": "code_lockout", "route": route})
            await _alert_lockout(request)
        _log(route, 404, reason="token")
        return _not_found()
    retry = guard.allow(now)
    if retry is not None:
        _log(route, 429, reason="rate")
        return _error(429, "rate_limited", **{"Retry-After": str(max(1, int(retry)))})

    body = await _body(request)
    allowed_keys = {"status": set(), "tree": {"root"}, "file": {"path"}}[route]
    if body is None or not set(body) <= allowed_keys or (route == "file" and "path" not in body):
        _log(route, 400, reason="body")
        return _error(400, "bad_request")

    async with request.app["sessionmaker"]() as session:
        window = await code_access.current(session, clock)
        read = 0 if window is None else await code_access.files_read(session, window.id)
    if route == "status":
        _log(route, 200, window_id=None if window is None else window.id)
        if window is None or window.expires_at is None:
            return web.json_response({"open": False})
        return web.json_response(
            {
                "open": True,
                "expires_at": window.expires_at.isoformat(),
                "roots": list(window.roots),
                "files_read": read,
                "files_left": max(0, settings.CODE_FILES_PER_WINDOW - read),
            }
        )
    if window is None:
        _log(route, 403, reason="no_window")
        return _error(403, "no_window")

    if route == "tree":
        return await _serve_tree(request, window, body)
    return await _serve_file(request, window, body, read)


async def _serve_tree(request: web.Request, window: code_access.Window, body: dict) -> web.Response:
    asked = list(window.roots)
    if "root" in body:
        root = code_access.parse_rel(body["root"]) if isinstance(body["root"], str) else None
        if root is None:
            _log("tree", 400, window_id=window.id, reason="root")
            return _error(400, "bad_request")
        if not code_access.covers(window.roots, root):
            _log("tree", 404, window_id=window.id, reason="outside")
            return _error(404, "not_found")
        asked = [root]
    try:
        tree = await _client(request).code_tree(asked)
    except VaultError as exc:
        response = _vault_failure("tree", exc.code)
        _log("tree", response.status, window_id=window.id, error_code=exc.code)
        return response
    files = [
        {"path": e.path, "class": e.note_class, "bytes": e.bytes, "mtime": e.mtime}
        for e in tree.files
        if code_access.parse_rel(e.path) == e.path and code_access.covers(asked, e.path)
    ]
    payload = json.dumps({"files": files, "truncated": tree.truncated}, ensure_ascii=False)
    await _record(request, window, "tree", len(files), len(payload.encode("utf-8")))
    _log(
        "tree", 200, window_id=window.id, count=len(files), dropped=len(tree.files) - len(files)
    )
    return web.Response(text=payload, content_type="application/json")


async def _serve_file(
    request: web.Request, window: code_access.Window, body: dict, read: int
) -> web.Response:
    settings: Settings = request.app["settings"]
    raw = body["path"]
    path = code_access.parse_rel(raw) if isinstance(raw, str) else None
    if path is None:
        _log("file", 400, window_id=window.id, reason="path")
        return _error(400, "bad_request")
    if not code_access.covers(window.roots, path):
        _log("file", 404, window_id=window.id, reason="outside")
        return _error(404, "not_found")
    if read >= settings.CODE_FILES_PER_WINDOW:
        _log("file", 429, window_id=window.id, reason="window_cap", count=read)
        return _error(429, "window_cap")
    try:
        note = await _client(request).code_file(path, list(window.roots))
    except VaultError as exc:
        response = _vault_failure("file", exc.code)
        _log("file", response.status, window_id=window.id, error_code=exc.code)
        return response
    payload = json.dumps(
        {"path": note.path, "class": note.note_class, "sha256": note.sha256, "text": note.text},
        ensure_ascii=False,
    )
    size = len(payload.encode("utf-8"))
    await _record(request, window, "file", 1, size)
    _log("file", 200, window_id=window.id, count=1, bytes=size)
    return web.Response(text=payload, content_type="application/json")


async def _record(
    request: web.Request, window: code_access.Window, route: str, files: int, size: int
) -> None:
    async with request.app["sessionmaker"]() as session:
        first = await code_access.record_read(
            session, request.app["clock"], window.id, route, files, size
        )
    if first:
        await _notify(request, window)


async def handle_status(request: web.Request) -> web.StreamResponse:
    return await _serve(request, "status")


async def handle_tree(request: web.Request) -> web.StreamResponse:
    return await _serve(request, "tree")


async def handle_file(request: web.Request) -> web.StreamResponse:
    return await _serve(request, "file")


def register(app: web.Application, settings: Settings) -> None:
    """Add the three routes. Every method is routed so that a GET is the
    same bare 404 as a wrong token, never aiohttp's 405 (which would
    say the route exists)."""
    del settings  # read per request, so a test can flip it on one app
    app[GUARD_KEY] = Guard()
    app.router.add_route("*", f"{PREFIX}/status", handle_status)
    app.router.add_route("*", f"{PREFIX}/tree", handle_tree)
    app.router.add_route("*", f"{PREFIX}/file", handle_file)
