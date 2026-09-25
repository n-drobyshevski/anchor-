"""The HTTP API the bot talks to (plan section 5.4).

All JSON. Every route except `/healthz` requires `Authorization: Bearer
<VAULT_API_TOKEN>`, compared with `hmac.compare_digest`; anything else
gets a bare 401.

**Status codes are part of the security boundary:**

- 400: the path (or body) is malformed. Says nothing about the vault.
- 403: a write, delete or purge on a path that is not writable, or one
  that crosses a symlink. The bot's code should never produce one.
- 404: *every* read refusal. A note that is unclassified or `never`,
  any note while `Anchor/settings.md` is unusable, the settings file
  itself, a note that does not exist, a dot-folder, a symlink, a
  folder: all the same 404 with the same body, so the bot cannot probe
  for what it may not see.
- 412: compare-and-swap failed (see store.py).
- 422: a file in Anchor's own folders that is not UTF-8. It is Anchor's
  scope, so it exists as far as the bot is concerned, but it cannot be
  returned as text.

**Logs** carry the method, the route template (`/v1/file`, never the
query), the status and the latency. Never a path or content.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
from typing import Any, Protocol

from aiohttp import web

from vaultd import classes, frontmatter, paths
from vaultd.config import BODY_MAX_BYTES, NOTE_MAX_BYTES
from vaultd.manifest import Manifest
from vaultd.store import Conflict, Missing, Store

logger = logging.getLogger("vaultd.api")

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NOT_FOUND = {"error": "not_found"}


class StatusSource(Protocol):
    def snapshot(self) -> dict[str, Any]: ...


STORE_KEY = web.AppKey("store", Store)
MANIFEST_KEY = web.AppKey("manifest", Manifest)
LOCK_KEY = web.AppKey("write_lock", asyncio.Lock)
SCAN_LOCK_KEY = web.AppKey("scan_lock", asyncio.Lock)
STATUS_KEY = web.AppKey("status", object)
TOKEN_KEY = web.AppKey("token", bytes)


def _route_template(request: web.Request) -> str:
    route = request.match_info.route
    resource = route.resource if route is not None else None
    return resource.canonical if resource is not None else "unmatched"


@web.middleware
async def log_requests(request: web.Request, handler):
    started = time.monotonic()
    status = 500
    try:
        response = await handler(request)
        status = response.status
        return response
    except web.HTTPException as exc:
        status = exc.status
        raise
    finally:
        logger.info(
            "request",
            extra={
                "method": request.method,
                "route": _route_template(request),
                "status": status,
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )


@web.middleware
async def require_token(request: web.Request, handler):
    if request.path == "/healthz":
        return await handler(request)
    expected = b"Bearer " + request.app[TOKEN_KEY]
    got = request.headers.get("Authorization", "").encode("utf-8", "surrogateescape")
    if not hmac.compare_digest(got, expected):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


def _json_error(code: str, status: int) -> web.Response:
    return web.json_response({"error": code}, status=status)


def _rel_from_query(request: web.Request) -> str:
    raw = request.query.get("path", "")
    return paths.parse_rel(raw)


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app[STATUS_KEY].snapshot())


async def manifest(request: web.Request) -> web.Response:
    async with request.app[SCAN_LOCK_KEY]:
        scan = await asyncio.to_thread(request.app[MANIFEST_KEY].scan)
    return web.json_response(
        {"files": [e.as_json() for e in scan.entries], "summary": scan.summary.as_json()}
    )


def _read_for_bot(store: Store, rel: str) -> tuple[str, str | None, bytes] | None:
    """(scope, class, bytes) if the manifest would list this path, else None.

    The class is recomputed here, at read time, from the note's bytes and
    the settings file as they are now, through the same
    classes.effective_class the manifest uses.
    """
    if paths.has_dot_segment(rel) or not rel.endswith(".md") or classes.is_settings_file(rel):
        return None
    try:
        data = paths.read_file(store.vault_path, rel)
    except paths.Refused:
        return None
    if data is None:
        return None
    if paths.is_writable(rel):
        return "anchor", None, data
    if len(data) > NOTE_MAX_BYTES:
        return None
    rules = classes.load_rules(store.vault_path)
    resolved = classes.effective_class(rel, frontmatter.note_mark(data), rules)
    if resolved.note_class is None:
        return None
    return "note", resolved.note_class, data


async def get_file(request: web.Request) -> web.Response:
    try:
        rel = _rel_from_query(request)
    except paths.Malformed:
        return _json_error("malformed_path", 400)
    found = await asyncio.to_thread(_read_for_bot, request.app[STORE_KEY], rel)
    if found is None:
        return web.json_response(_NOT_FOUND, status=404)
    scope, note_class, data = found
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return _json_error("not_utf8", 422)
    body = {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "content": content}
    if scope == "note":
        body["class"] = note_class
    return web.json_response(body)


async def put_file(request: web.Request) -> web.Response:
    try:
        rel = _rel_from_query(request)
    except paths.Malformed:
        return _json_error("malformed_path", 400)
    if not paths.is_writable(rel):
        return _json_error("not_writable", 403)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _json_error("bad_body", 400)
    if not isinstance(body, dict) or set(body) != {"content", "if_sha256"}:
        return _json_error("bad_body", 400)
    content, if_sha = body["content"], body["if_sha256"]
    if not isinstance(content, str):
        return _json_error("bad_body", 400)
    if if_sha is not None and not (isinstance(if_sha, str) and _SHA_RE.match(if_sha)):
        return _json_error("bad_body", 400)
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        return _json_error("bad_body", 400)
    if len(data) > BODY_MAX_BYTES:
        return _json_error("too_large", 413)
    store = request.app[STORE_KEY]
    async with request.app[LOCK_KEY]:
        try:
            new_sha = await asyncio.to_thread(store.put, rel, data, if_sha)
        except Conflict:
            return _json_error("precondition_failed", 412)
        except paths.Refused:
            return _json_error("not_writable", 403)
        except OSError:
            return _json_error("io_error", 500)
    return web.json_response({"sha256": new_sha})


async def delete_file(request: web.Request) -> web.Response:
    try:
        rel = _rel_from_query(request)
    except paths.Malformed:
        return _json_error("malformed_path", 400)
    if not paths.is_writable(rel):
        return _json_error("not_writable", 403)
    if_sha = request.query.get("if_sha256", "")
    if not _SHA_RE.match(if_sha):
        return _json_error("bad_body", 400)
    store = request.app[STORE_KEY]
    async with request.app[LOCK_KEY]:
        try:
            await asyncio.to_thread(store.delete, rel, if_sha)
        except Missing:
            return web.json_response(_NOT_FOUND, status=404)
        except Conflict:
            return _json_error("precondition_failed", 412)
        except paths.Refused:
            return _json_error("not_writable", 403)
        except OSError:
            return _json_error("io_error", 500)
    return web.json_response({"deleted": True})


async def purge(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    async with request.app[LOCK_KEY]:
        try:
            count = await asyncio.to_thread(store.purge)
        except paths.Refused:
            return _json_error("not_writable", 403)
        except OSError:
            return _json_error("io_error", 500)
    logger.info("purge", extra={"count": count})
    return web.json_response({"deleted": count})


def make_app(*, token: str, store: Store, manifest_: Manifest, status_source: StatusSource) -> web.Application:
    app = web.Application(
        middlewares=[log_requests, require_token],
        client_max_size=BODY_MAX_BYTES + 4096,
    )
    app[TOKEN_KEY] = token.encode("utf-8")
    app[STORE_KEY] = store
    app[MANIFEST_KEY] = manifest_
    app[LOCK_KEY] = asyncio.Lock()
    app[SCAN_LOCK_KEY] = asyncio.Lock()
    app[STATUS_KEY] = status_source
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/v1/status", status)
    app.router.add_get("/v1/manifest", manifest)
    app.router.add_get("/v1/file", get_file)
    app.router.add_put("/v1/file", put_file)
    app.router.add_delete("/v1/file", delete_file)
    app.router.add_post("/v1/purge", purge)
    return app
