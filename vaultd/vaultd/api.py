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
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from aiohttp import web

from vaultd import classes, echo, frontmatter, graph, knowledge, paths
from vaultd import limits as limits_mod
from vaultd.config import BODY_MAX_BYTES, ECHO_UNDOS_PER_HOUR, NOTE_MAX_BYTES
from vaultd.manifest import Manifest
from vaultd.store import Conflict, Missing, Store
from vaultd.undo import CLAUDE, ECHO, WRITERS, CapExceeded, FileEntry, UndoStore

logger = logging.getLogger("vaultd.api")

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CHANGESET_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NOT_FOUND = {"error": "not_found"}


class StatusSource(Protocol):
    def snapshot(self) -> dict[str, Any]: ...


STORE_KEY = web.AppKey("store", Store)
MANIFEST_KEY = web.AppKey("manifest", Manifest)
LOCK_KEY = web.AppKey("write_lock", asyncio.Lock)
SCAN_LOCK_KEY = web.AppKey("scan_lock", asyncio.Lock)
STATUS_KEY = web.AppKey("status", object)
TOKEN_KEY = web.AppKey("token", bytes)
UNDO_KEY = web.AppKey("undo_store", UndoStore)
CLOCK_KEY = web.AppKey("clock", object)


def _refused() -> web.Response:
    """The one 403, empty body, every knowledge-write acceptance failure gets."""
    return web.Response(status=403)


def _log_refused(request: web.Request, reason: str) -> None:
    """Log which rule refused -- the HTTP response never carries this.

    One record per refusal, `reason` a code from `undo.REFUSAL_REASONS`
    (never a path, folder name or title).
    """
    logger.info(
        "knowledge refused",
        extra={"event": "knowledge_refused", "route": _route_template(request), "reason": reason},
    )


def _now_iso(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    undo_store = request.app[UNDO_KEY]
    async with request.app[LOCK_KEY]:
        try:
            count = await asyncio.to_thread(store.purge)
        except paths.Refused:
            return _json_error("not_writable", 403)
        except OSError:
            return _json_error("io_error", 500)
        await asyncio.to_thread(undo_store.purge)
    logger.info("purge", extra={"count": count})
    return web.json_response({"deleted": count})


# -- knowledge: the class boundary Claude cannot cross ----------------------


async def get_knowledge(request: web.Request) -> web.Response:
    try:
        rel = _rel_from_query(request)
    except paths.Malformed:
        return _json_error("malformed_path", 400)
    data, reason = await asyncio.to_thread(_read_knowledge, request.app[STORE_KEY], rel)
    if data is None:
        _log_refused(request, reason)
        return web.json_response(_NOT_FOUND, status=404)
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        _log_refused(request, "bad_utf8")
        return web.json_response(_NOT_FOUND, status=404)
    return web.json_response(
        {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "content": content, "class": reason}
    )


def _read_knowledge(store: Store, rel: str) -> tuple[bytes | None, str]:
    """(bytes, class) if `rel` is a readable knowledge or lens note, else (None, reason).

    Lens notes read like knowledge (lens plan section 4); the class goes
    back with the content, so a caller can tell the read-only ones
    apart. `reason` is only ever looked at when the first element is
    None (the 404 path); the same closed set of codes
    `knowledge.Refused` uses.
    """
    path_reason = knowledge.candidate_path_reason(rel)
    if path_reason is not None:
        return None, path_reason
    try:
        data = paths.read_file(store.vault_path, rel)
    except paths.Refused:
        return None, "symlink"
    if data is None:
        return None, "missing"
    if len(data) > NOTE_MAX_BYTES:
        return None, "too_large"
    rules = classes.load_rules(store.vault_path)
    if rules.state == "invalid":
        return None, "settings_invalid"
    note_class = knowledge._class_of(rel, data, rules)  # noqa: SLF001 - same package
    if note_class not in classes.READABLE_KNOWLEDGE:
        return None, "not_knowledge"
    return data, note_class


def _parse_json_body(body: Any, keys: frozenset[str]) -> dict | None:
    if not isinstance(body, dict) or set(body) != keys:
        return None
    return body


async def put_knowledge(request: web.Request) -> web.Response:
    try:
        rel = _rel_from_query(request)
    except paths.Malformed:
        return _json_error("malformed_path", 400)
    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _json_error("bad_body", 400)
    body = _parse_json_body(raw, frozenset({"content", "if_sha256", "changeset"}))
    if body is None:
        return _json_error("bad_body", 400)
    content, if_sha, changeset = body["content"], body["if_sha256"], body["changeset"]
    if not isinstance(content, str):
        return _json_error("bad_body", 400)
    if if_sha is not None and not (isinstance(if_sha, str) and _SHA_RE.match(if_sha)):
        return _json_error("bad_body", 400)
    if not (isinstance(changeset, str) and _CHANGESET_RE.match(changeset)):
        return _json_error("bad_body", 400)

    store = request.app[STORE_KEY]
    undo_store = request.app[UNDO_KEY]
    now = lambda: _now_iso(request.app[CLOCK_KEY])  # noqa: E731
    async with request.app[LOCK_KEY]:
        try:
            await asyncio.to_thread(undo_store.precheck, changeset, "write", 1)
        except CapExceeded as exc:
            _log_refused(request, exc.reason)
            return _refused()
        try:
            new_folders = await asyncio.to_thread(knowledge.pending_new_folders, store, rel, if_sha)
        except knowledge.Refused as exc:
            _log_refused(request, exc.reason)
            return _refused()
        if new_folders:
            try:
                await asyncio.to_thread(undo_store.precheck_folders, changeset, len(new_folders))
            except CapExceeded as exc:
                _log_refused(request, exc.reason)
                return _refused()
        try:
            new_sha, pre_image = await asyncio.to_thread(
                knowledge.perform_put, store, rel, content, if_sha, now=now
            )
        except knowledge.Refused as exc:
            _log_refused(request, exc.reason)
            return _refused()
        except Missing:
            return web.json_response(_NOT_FOUND, status=404)
        except Conflict:
            return _json_error("precondition_failed", 412)
        except OSError:
            return _json_error("io_error", 500)
        if new_folders:
            await asyncio.to_thread(undo_store.append_folders, changeset, "write", list(new_folders))
        await asyncio.to_thread(undo_store.append, changeset, "write", [FileEntry(rel, pre_image, new_sha)])
    logger.info("knowledge_write", extra={"event": "knowledge_put"})
    return web.json_response({"sha256": new_sha, "folders_created": len(new_folders)})


async def rename_knowledge(request: web.Request) -> web.Response:
    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _json_error("bad_body", 400)
    body = _parse_json_body(raw, frozenset({"path", "new_path", "if_sha256", "changeset"}))
    if body is None:
        return _json_error("bad_body", 400)
    try:
        old_rel = paths.parse_rel(body["path"]) if isinstance(body["path"], str) else None
        new_rel = paths.parse_rel(body["new_path"]) if isinstance(body["new_path"], str) else None
    except paths.Malformed:
        return _json_error("malformed_path", 400)
    if old_rel is None or new_rel is None:
        return _json_error("bad_body", 400)
    if_sha, changeset = body["if_sha256"], body["changeset"]
    if not (isinstance(if_sha, str) and _SHA_RE.match(if_sha)):
        return _json_error("bad_body", 400)
    if not (isinstance(changeset, str) and _CHANGESET_RE.match(changeset)):
        return _json_error("bad_body", 400)

    store = request.app[STORE_KEY]
    undo_store = request.app[UNDO_KEY]
    now = lambda: _now_iso(request.app[CLOCK_KEY])  # noqa: E731
    async with request.app[LOCK_KEY]:
        try:
            plan = await asyncio.to_thread(knowledge.plan_rename, store.vault_path, old_rel, new_rel, if_sha)
        except knowledge.Refused as exc:
            _log_refused(request, exc.reason)
            return _refused()
        except Missing:
            return web.json_response(_NOT_FOUND, status=404)
        except Conflict:
            return _json_error("precondition_failed", 412)
        n_files = 1 + len(plan.backlinks)
        try:
            await asyncio.to_thread(undo_store.precheck_moves, changeset, n_files)
        except CapExceeded as exc:
            _log_refused(request, exc.reason)
            return _refused()
        if plan.new_folders:
            try:
                await asyncio.to_thread(undo_store.precheck_folders, changeset, len(plan.new_folders))
            except CapExceeded as exc:
                _log_refused(request, exc.reason)
                return _refused()
        try:
            entries = await asyncio.to_thread(knowledge.perform_rename, store, plan, now=now)
        except knowledge.Refused as exc:
            _log_refused(request, exc.reason)
            return _refused()
        except knowledge.RenameRaced:
            return _json_error("precondition_failed", 412)
        except knowledge.MidRenameFailure as exc:
            # The vault is left with a duplicate, not a loss; record what
            # is certain (the new path was created) so undoing this
            # changeset can still remove it. One file moved (the new
            # path exists now), even though the old one could not be
            # removed.
            await asyncio.to_thread(undo_store.append_move, changeset, "write", exc.entries, 1)
            return _json_error("io_error", 500)
        if plan.new_folders:
            await asyncio.to_thread(undo_store.append_folders, changeset, "write", list(plan.new_folders))
        await asyncio.to_thread(undo_store.append_move, changeset, "write", entries, n_files)
    logger.info("knowledge_rename", extra={"event": "knowledge_rename", "count": len(entries)})
    return web.json_response(
        {
            "path": entries[0].path,
            "sha256": entries[0].written_sha256,
            "relinked": len(entries) - 2,
            "folders_created": len(plan.new_folders),
            "files_moved": len(entries) - 1,
        }
    )


async def get_knowledge_tree(request: web.Request) -> web.Response:
    """`GET /v1/knowledge/tree` (rev. 3, BUILD item 4): what Claude sees
    before it writes -- knowledge folders and note titles, no bodies.
    No refusal shape here: an unusable settings file just answers with
    empty lists (`knowledge.build_tree`), the same as everywhere else."""
    store = request.app[STORE_KEY]
    tree = await asyncio.to_thread(_build_tree, store)
    return web.json_response(tree)


def _build_tree(store: Store) -> dict:
    rules = classes.load_rules(store.vault_path)
    return knowledge.build_tree(store.vault_path, rules)


async def get_knowledge_graph(request: web.Request) -> web.Response:
    """`GET /v1/knowledge/graph` (lens plan section 4): knowledge and lens
    notes and the links between them, built by graph.py. The log line
    carries counts only, never a path, title or link text."""
    store = request.app[STORE_KEY]
    body = await asyncio.to_thread(_build_graph, store)
    logger.info(
        "knowledge_graph",
        extra={"event": "knowledge_graph", "count": len(body["nodes"])},
    )
    return web.json_response(body)


def _build_graph(store: Store) -> dict:
    rules = classes.load_rules(store.vault_path)
    return graph.build_graph(store.vault_path, rules)


async def put_echo_inbox(request: web.Request) -> web.Response:
    """`PUT /v1/echo/inbox` (lens L4, echo.py): Echo's one write, a new
    knowledge note in the inbox. Body `{name, content, changeset}`; the
    answer `{name, sha256, replayed}`, where `name` is the basename
    vaultd chose (`name 2` .. `name 9` when taken). A refusal is the
    same bare 403 as the knowledge routes', its reason in the log only.
    The log line carries no name, path or content."""
    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _json_error("bad_body", 400)
    body = _parse_json_body(raw, frozenset({"name", "content", "changeset"}))
    if body is None:
        return _json_error("bad_body", 400)
    name, content, changeset = body["name"], body["content"], body["changeset"]
    if not isinstance(name, str) or not isinstance(content, str):
        return _json_error("bad_body", 400)
    if not (isinstance(changeset, str) and _CHANGESET_RE.match(changeset)):
        return _json_error("bad_body", 400)

    store = request.app[STORE_KEY]
    undo_store = request.app[UNDO_KEY]
    now = lambda: _now_iso(request.app[CLOCK_KEY])  # noqa: E731
    async with request.app[LOCK_KEY]:
        try:
            replay = await asyncio.to_thread(undo_store.echo_replay, changeset)
        except CapExceeded as exc:
            _log_refused(request, exc.reason)
            return _refused()
        if replay is not None:
            rel, sha = replay
            logger.info("echo_write", extra={"event": "echo_replayed"})
            return web.json_response({"name": rel.rsplit("/", 1)[-1], "sha256": sha, "replayed": True})
        try:
            planned = await asyncio.to_thread(echo.plan, store, name, content, now=now)
            await asyncio.to_thread(undo_store.precheck_echo, changeset)
            rel, sha = await asyncio.to_thread(echo.perform, store, planned)
        except (knowledge.Refused, CapExceeded) as exc:
            _log_refused(request, exc.reason)
            return _refused()
        except OSError:
            return _json_error("io_error", 500)
        if planned.new_folders:
            await asyncio.to_thread(
                undo_store.append_folders, changeset, "write", list(planned.new_folders), writer=ECHO
            )
        await asyncio.to_thread(
            undo_store.append, changeset, "write", [FileEntry(rel, None, sha)], writer=ECHO
        )
    logger.info("echo_write", extra={"event": "echo_put", "count": len(planned.new_folders)})
    return web.json_response({"name": rel.rsplit("/", 1)[-1], "sha256": sha, "replayed": False})


async def get_changes(request: web.Request) -> web.Response:
    changes = await asyncio.to_thread(request.app[UNDO_KEY].list_changes)
    return web.json_response({"changes": changes})


async def undo_changeset(request: web.Request) -> web.Response:
    """`POST /v1/undo?changeset=<id>[&writer=claude|echo]`. The writer
    (default `claude`) must be the changeset's own, in both directions
    (lens L4: Claude's undo tool can never take back Echo's note, nor
    `/lens undo` a Claude write), and each writer's undos count against
    its own hourly cap: the user's `undos_per_hour` for Claude, the
    constant `ECHO_UNDOS_PER_HOUR` for Echo."""
    changeset = request.query.get("changeset", "")
    if not _CHANGESET_RE.match(changeset):
        return _json_error("bad_body", 400)
    writer = request.query.get("writer", CLAUDE)
    if writer not in WRITERS:
        return _json_error("bad_body", 400)
    store = request.app[STORE_KEY]
    undo_store = request.app[UNDO_KEY]
    async with request.app[LOCK_KEY]:
        kind = await asyncio.to_thread(undo_store.kind_of, changeset)
        if kind is None:
            return web.json_response(_NOT_FOUND, status=404)
        if kind == "undo":
            _log_refused(request, "undo_of_undo")
            return _refused()
        if await asyncio.to_thread(undo_store.writer_of, changeset) != writer:
            _log_refused(request, "changeset_writer_mismatch")
            return _refused()
        if writer == ECHO:
            undos_per_hour, cap_reason = ECHO_UNDOS_PER_HOUR, "cap_echo"
        else:
            undos_per_hour = (await asyncio.to_thread(undo_store.limits.get)).undos_per_hour
            cap_reason = "cap_undos"
        if await asyncio.to_thread(undo_store.count_recent, "undo", writer) >= undos_per_hour:
            _log_refused(request, cap_reason)
            return _refused()
        entries = await asyncio.to_thread(undo_store.files_of, changeset)
        restored = 0
        refused = 0
        recorded: list[FileEntry] = []
        # Reverse chronological: a file written twice in one changeset
        # must have its later write undone first, so each step's CAS
        # check lines up with what the step before it just restored.
        for entry in reversed(entries):
            ok, record = await asyncio.to_thread(knowledge.undo_one, store, entry)
            if ok:
                restored += 1
                if record is not None:
                    recorded.append(record)
            else:
                refused += 1
        # Folders this changeset created (rev. 3): removed deepest first,
        # only if still empty after the file restores above -- a
        # non-empty one (a later write, or your own new file, landed in
        # it) is silently left in place.
        folders = await asyncio.to_thread(undo_store.folders_of, changeset)
        for folder in reversed(folders):
            await asyncio.to_thread(knowledge.remove_folder_if_empty, store, folder)
        if recorded:
            undo_id = undo_store.new_undo_id()
            await asyncio.to_thread(undo_store.append, undo_id, "undo", recorded, writer=writer)
            await asyncio.to_thread(undo_store.mark_undone, changeset)
    logger.info("knowledge_undo", extra={"event": "knowledge_undo", "count": restored})
    return web.json_response({"restored": restored, "refused": refused})


def _limits_json(current: limits_mod.Limits) -> dict:
    return {
        "values": current.as_json(),
        "defaults": limits_mod.DEFAULTS.as_json(),
        "bounds": {k: [spec.min, spec.max] for k, spec in limits_mod.SPECS.items()},
    }


async def get_limits(request: web.Request) -> web.Response:
    current = await asyncio.to_thread(request.app[UNDO_KEY].limits.get)
    return web.json_response(_limits_json(current))


async def put_limits(request: web.Request) -> web.Response:
    """Replace the caps with the body's keys over the defaults (never
    over the stored values): the bot always sends its full set, so a
    key it leaves out is a key it wants back at the default."""
    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _json_error("bad_body", 400)
    try:
        new = limits_mod.validate(raw)
    except limits_mod.Invalid:
        return _json_error("bad_body", 400)
    store = request.app[UNDO_KEY].limits
    async with request.app[LOCK_KEY]:
        await asyncio.to_thread(store.put, new)
    logger.info("limits_set", extra={"event": "limits_set"})
    return web.json_response(_limits_json(new))


def make_app(
    *,
    token: str,
    store: Store,
    manifest_: Manifest,
    status_source: StatusSource,
    undo_store: UndoStore,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> web.Application:
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
    app[UNDO_KEY] = undo_store
    app[CLOCK_KEY] = clock
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/v1/status", status)
    app.router.add_get("/v1/manifest", manifest)
    app.router.add_get("/v1/file", get_file)
    app.router.add_put("/v1/file", put_file)
    app.router.add_delete("/v1/file", delete_file)
    app.router.add_post("/v1/purge", purge)
    app.router.add_get("/v1/knowledge", get_knowledge)
    app.router.add_put("/v1/knowledge", put_knowledge)
    app.router.add_post("/v1/knowledge/rename", rename_knowledge)
    app.router.add_get("/v1/knowledge/tree", get_knowledge_tree)
    app.router.add_get("/v1/knowledge/graph", get_knowledge_graph)
    app.router.add_put("/v1/echo/inbox", put_echo_inbox)
    app.router.add_get("/v1/changes", get_changes)
    app.router.add_post("/v1/undo", undo_changeset)
    app.router.add_get("/v1/limits", get_limits)
    app.router.add_put("/v1/limits", put_limits)
    return app
