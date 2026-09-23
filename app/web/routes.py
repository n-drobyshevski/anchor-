"""The web chat's HTTP surface: `setup_web(app)` registers every route
in the HTTP API contract (web-chat plan track 2, design sections 4, 5,
6, 7 and 10).

Reads every dependency off `app[...]` rather than taking them as
parameters, because by the time `setup_web` runs (app/main.py, only
when `WEB_UI_ENABLED`) `app["settings"]`, `app["sessionmaker"]`,
`app["clock"]` and `app["bot"]` (the *real* Bot -- the one the login
code and the lockout alert must go through, never the web sink) are
already set by `build_webhook_app`. `setup_web` itself only adds the
web-chat-specific state: the `WebHub`, the web-sink `Bot`, a fresh
`CodeStore` and `WebRateLimiter`, and app/web/security.py's middleware.

Every handler here is deliberately thin: auth and session validation
live in app/web/auth.py, the CSRF/header/size checks live in
app/web/security.py's middleware and `read_json_bounded`, and the
actual queueing of a web-origin message or button press is
app/web/ingress.py's job (track 1). This module's own work is mapping
between HTTP and those pieces -- reading the cookie, calling the right
function, and translating its result into the exact status codes and
bodies the HTTP API contract specifies.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import pathlib
import re
import secrets
import time
import uuid

from aiogram import Bot
from aiohttp import web
from sqlalchemy import or_, select

from app.config import Settings
from app.core.clock import Clock
from app.db.models import Message
from app.web import auth, ingress, security
from app.web import panels
from app.web.hub import TooManySubscribers, WebHub
from app.web.http import (
    _clear_pre_cookie,
    _clear_session_cookie,
    _cookie_settings,
    _json,
    _parse_positive_int,
    _rate_limited,
    _read_body,
    _session_token_valid,
    _set_pre_cookie,
    _set_session_cookie,
)
from app.web.ratelimit import (
    LOCKOUT_ALERT_TEXT,
    MAX_PENDING_WEB_ROWS,
    WebRateLimiter,
    pending_web_count,
)

logger = logging.getLogger(__name__)

STATIC_DIR = pathlib.Path(__file__).parent / "static"

HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 100
PRESS_DATA_MAX_BYTES = 64

# 1..4000 chars after strip; every C0 control character except tab and
# newline is refused -- design section 5's "text is 1 to 4000 characters
# after strip, with NUL and control characters other than \n and \t
# rejected." (checked against the *original* text, not the stripped
# copy, so a message that is only whitespace/control characters cannot
# sneak a rejected byte in ahead of the length check by hiding it in
# leading/trailing whitespace that strip() would have removed anyway --
# not that it matters for safety, only for consistency: reject on the
# same string either way.)
_DISALLOWED_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f]")

# A lone UTF-16 surrogate passes the control-character check above (it
# is not a control character) and Python's own str type, then fails
# asyncpg's UTF-8 encoding at the query boundary -- an unhandled 500
# whose aiohttp.server traceback includes the offending text as a SQL
# parameter repr (W3 finding, verified against POST /api/send and this
# module's own message-write path). app/web/panels/memory.py's
# `_valid_shape` carries the identical regex for the same reason.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")

CODE_MESSAGE = "Код входа в веб-Anchor: {code} ({minutes} мин). Если это не ты — /weblogout"

BLOCKED_SEND_REPLY = {"error": "blocked"}


def _log(event: str, route: str, **extra) -> None:
    logger.info(event, extra={"event": event, "route": route, **extra})


# --- static manifest --------------------------------------------------

# The only extensions a `/static/{path:.+}` request may ever resolve to
# (W1 plan step 2). `index.html` is deliberately not among them: it is
# served only at `/` (the `index` handler below), never reachable at
# `/static/index.html` -- omitting `.html` here is what enforces that,
# with no separate check needed.
_STATIC_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}

# 1 MiB: generous for every file this app actually ships (the largest
# vendored module is a few KB), and small enough that even a manifest
# built from a directory an attacker could somehow write into cannot
# turn `setup_web` into an unbounded-memory read.
MAX_STATIC_FILE_BYTES = 1024 * 1024


class _StaticFile:
    """One `/static/*` response, fully precomputed at `setup_web` time.

    `body` is the file's bytes -- read once, at startup, never touched
    again. `etag_value` is the bare sha256 hex (what `aiohttp`'s parsed
    `ETag.value` carries -- no quotes, no `W/` prefix, per RFC 9110);
    `etag` is that same value quoted, exactly as the `ETag` response
    header must be sent. Both change if and only if the content would.
    """

    __slots__ = ("body", "content_type", "etag", "etag_value")

    def __init__(self, body: bytes, content_type: str) -> None:
        self.body = body
        self.content_type = content_type
        self.etag_value = hashlib.sha256(body).hexdigest()
        self.etag = f'"{self.etag_value}"'


def _build_static_manifest(static_dir: pathlib.Path) -> dict[str, _StaticFile]:
    """Walk `static_dir` once and return an exact-match `{relative
    posix path: _StaticFile}` dict -- the whole reason `GET
    /static/{path:.+}` (below) never touches the filesystem per
    request: it is a dict lookup against this manifest, built once at
    startup, or a 404.

    Three things are deliberately excluded, none of them raising --
    each is simply left out of the manifest, which 404s it exactly like
    a path that was never real:
    - a symlink (`path.is_symlink()`), checked *before* `is_file()`,
      which itself follows symlinks and would otherwise happily read
      through one to wherever it points;
    - anything whose extension is not in `_STATIC_CONTENT_TYPES` --
      `index.html`, `vendor/VENDOR.lock`, a stray `.map`, or anything
      else that is not one of the three kinds this app ever serves;
    - a file over `MAX_STATIC_FILE_BYTES`.

    Called once, at `setup_web` time (this module's own docstring):
    the frontend track's files may not exist yet when this runs in some
    test processes, which is fine -- `static_dir.is_dir()` being False,
    or simply finding nothing under it, both yield an empty manifest,
    not an error.
    """
    manifest: dict[str, _StaticFile] = {}
    if not static_dir.is_dir():
        return manifest
    for path in sorted(static_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        content_type = _STATIC_CONTENT_TYPES.get(path.suffix.lower())
        if content_type is None:
            continue
        try:
            if path.stat().st_size > MAX_STATIC_FILE_BYTES:
                continue
            body = path.read_bytes()
        except OSError:
            continue
        rel = path.relative_to(static_dir).as_posix()
        manifest[rel] = _StaticFile(body=body, content_type=content_type)
    return manifest


async def static_file(request: web.Request) -> web.StreamResponse:
    """`GET /static/{path:.+}`: an exact-match lookup against the
    manifest `setup_web` built, or 404 -- never a filesystem read, and
    never anything resembling `..`-traversal, a percent-encoded
    variant, an absolute path or a directory, since none of those are
    ever a *key* the manifest contains (aiohttp decodes the path before
    handing it to this handler, but a decoded traversal string is still
    just a string this dict does not have).
    """
    manifest: dict[str, _StaticFile] = request.app["web_static_manifest"]
    entry = manifest.get(request.match_info["path"])
    if entry is None:
        raise web.HTTPNotFound()
    # aiohttp parses If-None-Match into a list of ETag(value, is_weak)
    # per RFC 9110 -- a weak comparison (a proxy that gzips the body and
    # rewrites the validator to W/"<sha>"), a comma-separated list, or
    # "*" (also from ETag.value, unquoted, same as entry.etag_value)
    # must all still 304. Comparing the raw header string against our
    # quoted etag (the old code's `==`) matched none of those, so a
    # compressing proxy/CDN in front of this app defeated revalidation
    # entirely -- every load re-downloaded every module.
    inm = request.if_none_match
    if inm is not None and any(tag.value in (entry.etag_value, "*") for tag in inm):
        return web.Response(status=304, headers={"ETag": entry.etag})
    return web.Response(
        body=entry.body, headers={"Content-Type": entry.content_type, "ETag": entry.etag}
    )


async def index(request: web.Request) -> web.StreamResponse:
    path = STATIC_DIR / "index.html"
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": "text/html; charset=utf-8"})


# --- GET /api/me -------------------------------------------------------


async def me(request: web.Request) -> web.Response:
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    token = request.cookies.get(auth.SESSION_COOKIE)
    async with sessionmaker() as session:
        authenticated = await auth.validate_session(session, clock, settings, token)
    if authenticated:
        return _json(200, {"authenticated": True, "stage": "none"})

    pre_token = request.cookies.get(auth.PRE_COOKIE)
    code_store = request.app["web_code_store"]
    stage = "code" if pre_token and code_store.pending(pre_token, clock) else "none"
    return _json(200, {"authenticated": False, "stage": stage})


# --- POST /api/auth/passphrase -----------------------------------------


async def _send_lockout_alert(bot: Bot, settings: Settings) -> None:
    try:
        await bot.send_message(chat_id=settings.ALLOWED_CHAT_ID, text=LOCKOUT_ALERT_TEXT)
    except Exception as exc:  # noqa: BLE001 - an alert send must never break the response
        logger.warning("web lockout alert failed", extra={"event": type(exc).__name__})


# A burst of concurrent POST /api/auth/passphrase requests must not each
# get to run their own scrypt hash: check_passphrase_lockout() used to
# run once per request, *before* the awaited scrypt call, so nothing
# counted an attempt as "in flight" and an unbounded number of guesses
# could race the 5-per-15-min check at once (a memory/CPU DoS -- each
# scrypt at N=2**17,r=8 allocates 128 MiB, and a burst of them stalls
# every other to_thread user in the process). `_PASSPHRASE_LOCK` (held
# on `app["web_passphrase_lock"]`) serializes verification end to end --
# the lockout re-check, the scrypt call, and recording the failure or
# success all happen inside the lock -- so at most one scrypt call ever
# runs at a time and at most PASSPHRASE_FAIL_LIMIT guesses land before
# the lockout applies, no matter how many requests arrive together.
# `_PASSPHRASE_MAX_WAITERS` bounds how many requests may queue for that
# lock at once; past it, a request is turned away with 429 immediately
# instead of piling up behind the lock forever.
_PASSPHRASE_MAX_WAITERS = 4
_PASSPHRASE_BUSY_RETRY_S = 2.0


async def _verify_passphrase_locked(
    *,
    settings: Settings,
    clock: Clock,
    bot: Bot,
    limiter: WebRateLimiter,
    code_store,
    passphrase: str,
) -> web.Response:
    """The whole check-then-act sequence, run under `web_passphrase_lock`.

    Re-running check_passphrase_lockout() here (not only before the lock
    was acquired) is what closes the race: a request that queued behind
    the lock while a sibling's failure just triggered a lockout must see
    that lockout too, not the stale "not locked out yet" answer it read
    before waiting.
    """
    retry = limiter.check_passphrase_lockout()
    if retry is not None:
        return _rate_limited(retry)

    ok = await auth.verify_passphrase(passphrase, settings.WEB_PASSPHRASE_HASH)
    if not ok:
        just_locked = limiter.record_passphrase_failure()
        _log("web_login_fail", "auth_passphrase")
        if just_locked:
            _log("web_lockout", "auth_passphrase")
            await _send_lockout_alert(bot, settings)
        return _json(401, {"error": "invalid"})

    limiter.record_passphrase_success()

    code_retry = limiter.check_code_send()
    if code_retry is not None:
        return _rate_limited(code_retry)

    pre_token = secrets.token_urlsafe(32)
    code = code_store.issue(pre_token, clock, settings.WEB_LOGIN_CODE_TTL_S)
    minutes = max(1, settings.WEB_LOGIN_CODE_TTL_S // 60)
    await bot.send_message(
        chat_id=settings.ALLOWED_CHAT_ID, text=CODE_MESSAGE.format(code=code, minutes=minutes)
    )
    _log("web_code_sent", "auth_passphrase")

    response = _json(200, {"next": "code"})
    _set_pre_cookie(response, pre_token, settings.WEB_LOGIN_CODE_TTL_S)
    return response


async def auth_passphrase(request: web.Request) -> web.Response:
    settings, sessionmaker, clock, bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    code_store = request.app["web_code_store"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    passphrase = body.get("passphrase")
    if not isinstance(passphrase, str) or not passphrase:
        return _json(400, {"error": "bad_request"})

    lock: asyncio.Lock = request.app["web_passphrase_lock"]
    waiters: dict = request.app["web_passphrase_waiters"]
    if lock.locked() and waiters["n"] >= _PASSPHRASE_MAX_WAITERS:
        return _rate_limited(_PASSPHRASE_BUSY_RETRY_S)
    waiters["n"] += 1
    try:
        async with lock:
            return await _verify_passphrase_locked(
                settings=settings,
                clock=clock,
                bot=bot,
                limiter=limiter,
                code_store=code_store,
                passphrase=passphrase,
            )
    finally:
        waiters["n"] -= 1


# --- POST /api/auth/code ------------------------------------------------


async def auth_code(request: web.Request) -> web.Response:
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    code_store = request.app["web_code_store"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    code = body.get("code")
    if not isinstance(code, str) or not code:
        return _json(400, {"error": "bad_request"})

    pre_token = request.cookies.get(auth.PRE_COOKIE)
    if not pre_token or not code_store.verify(pre_token, code, clock):
        _log("web_login_fail", "auth_code")
        return _json(401, {"error": "invalid"})

    async with sessionmaker() as session:
        token, _expires_at = await auth.create_session(session, clock, settings)
    _log("web_login_ok", "auth_code")

    response = _json(200, {"ok": True})
    _clear_pre_cookie(response)
    _set_session_cookie(response, token, settings)
    return response


# --- POST /api/auth/logout ----------------------------------------------


async def logout(request: web.Request) -> web.Response:
    _settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    token = request.cookies.get(auth.SESSION_COOKIE)
    async with sessionmaker() as session:
        await auth.revoke_session(session, token)
    _log("web_logout", "logout")
    response = web.Response(status=204)
    _clear_session_cookie(response)
    return response


# --- GET /api/history ----------------------------------------------------


async def history(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})

    _settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    before = _parse_positive_int(request.query.get("before"), None)
    limit = _parse_positive_int(request.query.get("limit"), HISTORY_DEFAULT_LIMIT)
    limit = min(limit, HISTORY_MAX_LIMIT)

    stmt = select(Message).where(or_(Message.role == "user", Message.sent_at.is_not(None)))
    if before is not None:
        stmt = stmt.where(Message.id < before)
    stmt = stmt.order_by(Message.id.desc()).limit(limit + 1)

    async with sessionmaker() as session:
        result = await session.execute(stmt)
        rows = list(result.scalars())

    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()  # oldest-first within the page, per the contract
    messages = [
        {
            "id": row.id,
            "role": row.role,
            "text": row.content,
            "kind": row.kind,
            "ts": row.created_at.isoformat(),
        }
        for row in rows
    ]
    return _json(200, {"messages": messages, "has_more": has_more})


# --- POST /api/send --------------------------------------------------------


def _valid_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not (1 <= len(stripped) <= 4000):
        return False
    return not _DISALLOWED_CONTROL_RE.search(value) and not _SURROGATE_RE.search(value)


def _valid_client_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4


async def send(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})

    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    limiter: WebRateLimiter = request.app["web_rate_limiter"]
    hub: WebHub = request.app["web_hub"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    text = body.get("text")
    client_key = body.get("client_key")
    if not _valid_text(text) or not _valid_client_key(client_key):
        return _json(400, {"error": "bad_request"})

    retry = limiter.check_send()
    if retry is not None:
        return _rate_limited(retry)

    stripped = text.strip()
    async with sessionmaker() as session:
        backlog = await pending_web_count(session)
        if backlog >= MAX_PENDING_WEB_ROWS:
            return _rate_limited(5.0)
        try:
            update_id = await ingress.send_text(
                session, settings=settings, text=stripped, client_key=client_key
            )
        except ingress.BlockedCommand:
            _log("web_rejected", "send")
            return _json(422, dict(BLOCKED_SEND_REPLY))

    # Mirror the user's own message to every live SSE stream, including
    # the sending tab's -- so a second open tab/device (which never sees
    # its own outgoing POSTs) shows it too. Published with the negative
    # `update_id` as its id: app.js registers the same id against its
    # optimistic bubble on a 202, so the sending tab's own copy of this
    # event is deduped rather than rendered twice.
    hub.publish_message(
        id=update_id, role="user", text=stripped, kind="chat", keyboard=None, ts=clock.now_utc()
    )

    return _json(202, {"update_id": update_id})


# --- POST /api/press -------------------------------------------------------


def _valid_press_data(value: object) -> bool:
    """Shape-only validation (size + no control characters); *content*
    is validated separately and authoritatively by ingress.press()'s
    allowlist check against what WebSinkSession actually issued (design
    section 2's "Button presses"). This function exists only to reject
    obvious garbage before a DB round-trip, not to second-guess the
    allowlist -- a narrower charset check here could reject a future,
    legitimate callback_data shape this track does not control.
    """
    if not isinstance(value, str) or not value:
        return False
    if len(value.encode("utf-8")) > PRESS_DATA_MAX_BYTES:
        return False
    return not _DISALLOWED_CONTROL_RE.search(value)


async def press(request: web.Request) -> web.Response:
    if not await _session_token_valid(request):
        return _json(401, {"error": "unauthenticated"})

    settings, sessionmaker, _clock, _bot = _cookie_settings(request)
    hub: WebHub = request.app["web_hub"]
    limiter: WebRateLimiter = request.app["web_rate_limiter"]

    body, error = await _read_body(request)
    if error is not None:
        return error
    message_id = body.get("message_id")
    data = body.get("data")
    if not isinstance(message_id, int) or isinstance(message_id, bool) or not _valid_press_data(data):
        return _json(400, {"error": "bad_request"})

    retry = limiter.check_press()
    if retry is not None:
        return _rate_limited(retry)

    async with sessionmaker() as session:
        try:
            await ingress.press(session, hub, settings=settings, message_id=message_id, data=data)
        except (ingress.BlockedCommand, ingress.PressRejected):
            _log("web_rejected", "press")
            return _json(409, {"error": "stale"})

    return _json(202, {})


# --- GET /api/events (SSE) --------------------------------------------------

SSE_KEEPALIVE_S = 15.0
SSE_SESSION_RECHECK_S = 60.0
SSE_MAX_LIFETIME_S = 30 * 60.0


def _format_sse(record) -> bytes:
    lines = [f"id: {record.seq}", f"event: {record.event}", f"data: {json.dumps(record.data)}"]
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _parse_last_event_id(request: web.Request) -> int | None:
    raw = request.headers.get("Last-Event-ID")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def events(request: web.Request) -> web.StreamResponse:
    settings, sessionmaker, clock, _bot = _cookie_settings(request)
    hub: WebHub = request.app["web_hub"]
    token = request.cookies.get(auth.SESSION_COOKIE)

    async with sessionmaker() as session:
        authed = await auth.validate_session(session, clock, settings, token)
    if not authed:
        return _json(401, {"error": "unauthenticated"})

    try:
        subscription = hub.subscribe(_parse_last_event_id(request))
    except TooManySubscribers:
        return _rate_limited(5.0)

    headers = security.build_headers(settings)
    headers["Content-Type"] = "text/event-stream"
    headers["Cache-Control"] = "no-store"
    headers["X-Accel-Buffering"] = "no"  # nginx/Railway: never buffer an SSE stream
    response = web.StreamResponse(status=200, headers=headers)
    await response.prepare(request)

    gen = subscription.events()
    pending = asyncio.ensure_future(gen.__anext__())
    started = time.monotonic()
    last_recheck = started
    try:
        while True:
            now = time.monotonic()
            remaining = SSE_MAX_LIFETIME_S - (now - started)
            if remaining <= 0:
                break
            timeout = min(SSE_KEEPALIVE_S, remaining)
            done, _pending = await asyncio.wait({pending}, timeout=timeout)
            if pending in done:
                try:
                    record = pending.result()
                except StopAsyncIteration:
                    break
                await response.write(_format_sse(record))
                pending = asyncio.ensure_future(gen.__anext__())
            else:
                await response.write(b": ka\n\n")

            now = time.monotonic()
            if now - last_recheck >= SSE_SESSION_RECHECK_S:
                last_recheck = now
                async with sessionmaker() as session:
                    still_ok = await auth.validate_session(session, clock, settings, token)
                if not still_ok:
                    break
    except ConnectionResetError:
        pass
    # asyncio.CancelledError is deliberately *not* caught here (it used
    # to be, alongside ConnectionResetError): aiohttp cancels this
    # handler's task itself on shutdown (AppRunner.cleanup's
    # server.shutdown(...)), and swallowing that cancellation instead of
    # letting it propagate is what made every open SSE stream hold up
    # the full shutdown_timeout. The `finally` below still runs (Python
    # always runs a `finally` while a CancelledError is propagating), so
    # the subscription and the pending future are cleaned up either way.
    finally:
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
            await pending
        subscription.close()

    return response


# --- registration ------------------------------------------------------


def setup_web(
    app: web.Application, *, hub: WebHub, web_bot: Bot, code_store: auth.CodeStore | None = None
) -> None:
    """Wire the web chat into `app`. Called only when WEB_UI_ENABLED
    (app/main.py), after `app["settings"]`/`app["sessionmaker"]`/
    `app["clock"]`/`app["bot"]` are already set.

    `code_store` is accepted rather than always built here so it can be
    the *same* instance app/main.py already threaded into build_router()
    for `/weblogout` -- two separate CodeStores would mean the Telegram
    kill switch clears one nobody's pending login code is ever actually
    in. `code_store=None` (every call site here in this module's own
    tests, which have no router/weblogout to share with) falls back to
    a fresh one, keeping this function usable standalone.
    """
    settings: Settings = app["settings"]
    clock: Clock = app["clock"]

    app["web_hub"] = hub
    app["web_bot"] = web_bot
    app["web_code_store"] = code_store if code_store is not None else auth.CodeStore()
    app["web_rate_limiter"] = WebRateLimiter(clock)
    app["web_passphrase_lock"] = asyncio.Lock()
    app["web_passphrase_waiters"] = {"n": 0}
    # Built once, here, not per-request: see _build_static_manifest's
    # docstring for why this is what keeps GET /static/{path:.+} off
    # the filesystem entirely. Built from whatever exists under
    # STATIC_DIR at this exact moment, so it picks up the frontend
    # track's app/**/*.js files whenever setup_web happens to run after
    # they have landed -- and an empty vendor/app dir (a test process
    # that never wrote them) just means an empty manifest, not an error.
    app["web_static_manifest"] = _build_static_manifest(STATIC_DIR)

    app.router.add_get("/", index)
    app.router.add_get("/static/{path:.+}", static_file)

    app.router.add_get("/api/me", me)
    app.router.add_post("/api/auth/passphrase", auth_passphrase)
    app.router.add_post("/api/auth/code", auth_code)
    app.router.add_post("/api/auth/logout", logout)
    app.router.add_get("/api/history", history)
    app.router.add_post("/api/send", send)
    app.router.add_post("/api/press", press)
    app.router.add_get("/api/events", events)

    # W2: state + proposals panels (app/web/panels/).
    panels.register(app)

    app.middlewares.append(security.build_middleware(settings))
