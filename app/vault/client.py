"""The bot's HTTP client for vaultd (phase-8 plan section 5.4).

One method per route, each returning plain data or raising VaultError
with a code from app/vault/errors.py. 8a calls only `status()`; the
rest exist because the API they mirror ships in 8a, and 8b-8d use them.

**8e: the bot trusts the class vaultd reports, and nothing looser.** A
note entry must carry `class` `personal` or `knowledge`; a missing or
foreign class is a protocol error, exactly like a bad scope, and an
Anchor-scope entry must carry none. The manifest's `summary` holds
counts and the settings file's state, never a path: the bot cannot know
the names of notes it may not see.

**What the client refuses to do.** No redirects (a 3xx is a bad
response, not a hop). No proxy or `.netrc` from the environment
(`trust_env=False`), so the bearer token only ever goes to VAULT_URL,
which check_runtime_settings has already pinned to the private network.
No cookies. A response larger than MAX_RESPONSE_BYTES is refused
rather than buffered. The timeout is a constant: a hung vault service
must cost a /vault command seconds, not a worker slot.

**Nothing here is logged** -- not the URL, not a path, not a body. The
callers log codes.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from dataclasses import dataclass
from typing import Any, Callable

import aiohttp

from app.config import Settings
from app.vault import errors
from app.vault.errors import VaultError

TIMEOUT_S = 5.0
# The manifest of a vault with a few thousand notes is well under 1 MB;
# a file body is capped at 64 KB by vaultd. Eight megabytes is slack,
# not a target.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

SessionOpener = Callable[[], aiohttp.ClientSession]


def open_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={"User-Agent": "AnchorBot/1.0 (vault client)"},
        trust_env=False,
    )


@dataclass(frozen=True)
class ServiceStatus:
    sync_running: bool
    restarts: int
    last_exit_code: int | None
    running_since: datetime.datetime | None


NOTE_CLASSES = ("personal", "knowledge")
SETTINGS_STATES = ("ok", "absent", "invalid")


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    sha256: str
    size: int
    scope: str
    # `personal` or `knowledge` for a note; None for Anchor's own files.
    note_class: str | None = None


@dataclass(frozen=True)
class NotesSummary:
    """What vaultd says about notes it does not list. Counts only."""

    conflict: int
    legacy_read: int
    unknown_value: int
    settings: str


@dataclass(frozen=True)
class Manifest:
    entries: list[ManifestEntry]
    summary: NotesSummary


@dataclass(frozen=True)
class FileContent:
    path: str
    sha256: str
    content: str
    note_class: str | None = None


@dataclass(frozen=True)
class KnowledgeContent:
    path: str
    sha256: str
    content: str


@dataclass(frozen=True)
class ChangeFile:
    path: str
    sha256: str | None


@dataclass(frozen=True)
class ChangeEntry:
    """One row of vaultd's `GET /v1/changes` index (W2b). No pre-image
    bytes ever reach the bot -- vaultd's own index route never returns
    them (vaultd/vaultd/undo.py's `list_changes`)."""

    id: str
    kind: str
    time: datetime.datetime
    undone: bool
    files: list[ChangeFile]


@dataclass(frozen=True)
class PutResult:
    """`PUT /v1/knowledge`'s body (rev. 3 adds `folders_created`): the
    new hash, and how many folders vaultd created on the way there
    (`app/web/claude_write.py` sums this into `claude_changeset.folders`)."""

    sha256: str
    folders_created: int


@dataclass(frozen=True)
class RenameResult:
    path: str
    sha256: str
    relinked: int
    # Rev. 3: folders vaultd created for the destination, and the total
    # files this rename counts against the move budget (the moved note
    # plus its rewritten backlinks -- `1 + relinked`, but read from
    # vaultd's own response rather than recomputed here).
    folders_created: int
    files_moved: int


@dataclass(frozen=True)
class UndoResult:
    restored: int
    refused: int


@dataclass(frozen=True)
class TreeNote:
    path: str
    title: str


@dataclass(frozen=True)
class Tree:
    """`GET /v1/knowledge/tree`'s body (rev. 3, BUILD item 4): the
    knowledge folders and note titles `list_tree` hands to Claude. No
    body text ever reaches this far."""

    folders: list[str]
    notes: list[TreeNote]
    truncated: bool


def _count(value: Any) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0)
    return value


def _note_class(scope: str, item: dict) -> str | None:
    """A note must say which class it is; Anchor's own files must not."""
    if scope == "anchor":
        _require("class" not in item)
        return None
    note_class = item.get("class")
    _require(isinstance(note_class, str) and note_class in NOTE_CLASSES)
    return note_class


def _summary(raw: Any) -> NotesSummary:
    _require(isinstance(raw, dict))
    settings = raw.get("settings")
    _require(isinstance(settings, str) and settings in SETTINGS_STATES)
    return NotesSummary(
        conflict=_count(raw.get("conflict")),
        legacy_read=_count(raw.get("legacy_read")),
        unknown_value=_count(raw.get("unknown_value")),
        settings=settings,
    )


def _status_error(status: int) -> str:
    if status == 401:
        return errors.UNAUTHORIZED
    if status == 404:
        return errors.NOT_FOUND
    if status == 412:
        return errors.CONFLICT
    if status in (400, 403, 413, 422):
        return errors.REFUSED
    return errors.UNAVAILABLE if status >= 500 else errors.BAD_RESPONSE


def _require(condition: bool) -> None:
    if not condition:
        raise VaultError(errors.BAD_RESPONSE)


def _parse_time(value: Any) -> datetime.datetime | None:
    if value is None:
        return None
    _require(isinstance(value, str))
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        raise VaultError(errors.BAD_RESPONSE) from None
    _require(parsed.tzinfo is not None)
    return parsed


class VaultClient:
    def __init__(self, base_url: str, token: str, *, opener: SessionOpener = open_session) -> None:
        self._base = base_url.strip().rstrip("/")
        self._token = token
        self._opener = opener

    @classmethod
    def from_settings(cls, settings: Settings) -> "VaultClient":
        return cls(settings.VAULT_URL, settings.VAULT_API_TOKEN)

    async def _request(
        self,
        method: str,
        route: str,
        *,
        params: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with self._opener() as session:
                async with session.request(
                    method,
                    self._base + route,
                    params=params,
                    json=body,
                    headers=headers,
                    allow_redirects=False,
                ) as resp:
                    raw = await resp.content.read(MAX_RESPONSE_BYTES + 1)
                    status = resp.status
        except asyncio.TimeoutError:
            raise VaultError(errors.UNAVAILABLE) from None
        except (aiohttp.ClientError, OSError):
            raise VaultError(errors.UNAVAILABLE) from None
        if status != 200:
            raise VaultError(_status_error(status))
        _require(len(raw) <= MAX_RESPONSE_BYTES)
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise VaultError(errors.BAD_RESPONSE) from None
        _require(isinstance(parsed, dict))
        return parsed

    async def status(self) -> ServiceStatus:
        data = await self._request("GET", "/v1/status")
        running = data.get("sync_running")
        restarts = data.get("restarts")
        exit_code = data.get("last_exit_code")
        _require(isinstance(running, bool))
        _require(isinstance(restarts, int) and not isinstance(restarts, bool) and restarts >= 0)
        _require(exit_code is None or (isinstance(exit_code, int) and not isinstance(exit_code, bool)))
        return ServiceStatus(
            sync_running=running,
            restarts=restarts,
            last_exit_code=exit_code,
            running_since=_parse_time(data.get("running_since")),
        )

    async def manifest(self) -> Manifest:
        data = await self._request("GET", "/v1/manifest")
        files = data.get("files")
        _require(isinstance(files, list))
        out = []
        for item in files:
            _require(isinstance(item, dict))
            path, sha, size, scope = (item.get(k) for k in ("path", "sha256", "size", "scope"))
            _require(isinstance(path, str) and isinstance(sha, str) and isinstance(size, int))
            _require(scope in ("anchor", "note"))
            out.append(
                ManifestEntry(path=path, sha256=sha, size=size, scope=scope, note_class=_note_class(scope, item))
            )
        return Manifest(entries=out, summary=_summary(data.get("summary")))

    async def get_file(self, path: str) -> FileContent:
        data = await self._request("GET", "/v1/file", params={"path": path})
        content, sha = data.get("content"), data.get("sha256")
        _require(isinstance(content, str) and isinstance(sha, str) and data.get("path") == path)
        note_class = data.get("class")
        _require(note_class is None or note_class in NOTE_CLASSES)
        return FileContent(path=path, sha256=sha, content=content, note_class=note_class)

    async def put_file(self, path: str, content: str, if_sha256: str | None) -> str:
        data = await self._request(
            "PUT", "/v1/file", params={"path": path}, body={"content": content, "if_sha256": if_sha256}
        )
        sha = data.get("sha256")
        _require(isinstance(sha, str))
        return sha

    async def delete_file(self, path: str, if_sha256: str) -> None:
        await self._request("DELETE", "/v1/file", params={"path": path, "if_sha256": if_sha256})

    async def purge(self) -> int:
        data = await self._request("POST", "/v1/purge")
        deleted = data.get("deleted")
        _require(isinstance(deleted, int) and not isinstance(deleted, bool))
        return deleted

    async def put_limits(self, values: dict[str, int]) -> None:
        """Replace vaultd's copy of Claude's write caps (`PUT /v1/limits`).
        `values` is the full set: a key left out goes back to vaultd's
        default."""
        data = await self._request("PUT", "/v1/limits", body=values)
        _require(isinstance(data.get("values"), dict))

    # -- knowledge: the class boundary Claude cannot cross (W2b) ------------

    async def get_knowledge(self, path: str) -> KnowledgeContent:
        data = await self._request("GET", "/v1/knowledge", params={"path": path})
        content, sha = data.get("content"), data.get("sha256")
        _require(isinstance(content, str) and isinstance(sha, str) and data.get("path") == path)
        return KnowledgeContent(path=path, sha256=sha, content=content)

    async def put_knowledge(
        self, path: str, content: str, if_sha256: str | None, changeset: str
    ) -> PutResult:
        data = await self._request(
            "PUT",
            "/v1/knowledge",
            params={"path": path},
            body={"content": content, "if_sha256": if_sha256, "changeset": changeset},
        )
        sha = data.get("sha256")
        _require(isinstance(sha, str))
        folders_created = data.get("folders_created")
        _require(
            isinstance(folders_created, int)
            and not isinstance(folders_created, bool)
            and folders_created >= 0
        )
        return PutResult(sha256=sha, folders_created=folders_created)

    async def rename_knowledge(
        self, path: str, new_path: str, if_sha256: str, changeset: str
    ) -> RenameResult:
        data = await self._request(
            "POST",
            "/v1/knowledge/rename",
            body={"path": path, "new_path": new_path, "if_sha256": if_sha256, "changeset": changeset},
        )
        new_sha, relinked = data.get("sha256"), data.get("relinked")
        _require(isinstance(new_sha, str))
        _require(isinstance(relinked, int) and not isinstance(relinked, bool) and relinked >= 0)
        folders_created, files_moved = data.get("folders_created"), data.get("files_moved")
        _require(
            isinstance(folders_created, int)
            and not isinstance(folders_created, bool)
            and folders_created >= 0
        )
        _require(isinstance(files_moved, int) and not isinstance(files_moved, bool) and files_moved >= 0)
        return RenameResult(
            path=data.get("path", new_path),
            sha256=new_sha,
            relinked=relinked,
            folders_created=folders_created,
            files_moved=files_moved,
        )

    async def knowledge_tree(self) -> Tree:
        data = await self._request("GET", "/v1/knowledge/tree")
        folders, notes, truncated = data.get("folders"), data.get("notes"), data.get("truncated")
        _require(isinstance(folders, list) and all(isinstance(f, str) for f in folders))
        _require(isinstance(notes, list))
        _require(isinstance(truncated, bool))
        out_notes = []
        for item in notes:
            _require(isinstance(item, dict))
            path, title = item.get("path"), item.get("title")
            _require(isinstance(path, str) and isinstance(title, str))
            out_notes.append(TreeNote(path=path, title=title))
        return Tree(folders=folders, notes=out_notes, truncated=truncated)

    async def list_changes(self) -> list[ChangeEntry]:
        data = await self._request("GET", "/v1/changes")
        changes = data.get("changes")
        _require(isinstance(changes, list))
        out = []
        for item in changes:
            _require(isinstance(item, dict))
            cid, kind, when, undone, files = (
                item.get(k) for k in ("id", "kind", "time", "undone", "files")
            )
            _require(isinstance(cid, str) and kind in ("write", "undo") and isinstance(undone, bool))
            _require(isinstance(files, list))
            parsed_files = []
            for f in files:
                _require(isinstance(f, dict))
                path, sha = f.get("path"), f.get("sha256")
                _require(isinstance(path, str) and (sha is None or isinstance(sha, str)))
                parsed_files.append(ChangeFile(path=path, sha256=sha))
            out.append(
                ChangeEntry(
                    id=cid, kind=kind, time=_parse_time(when), undone=undone, files=parsed_files
                )
            )
        return out

    async def undo_changeset(self, vault_ref: str) -> UndoResult:
        data = await self._request("POST", "/v1/undo", params={"changeset": vault_ref})
        restored, refused = data.get("restored"), data.get("refused")
        _require(isinstance(restored, int) and not isinstance(restored, bool) and restored >= 0)
        _require(isinstance(refused, int) and not isinstance(refused, bool) and refused >= 0)
        return UndoResult(restored=restored, refused=refused)
