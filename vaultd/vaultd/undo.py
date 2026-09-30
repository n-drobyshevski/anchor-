"""The undo store: pre-images kept outside the vault (write-plan section 6.2).

**Layout.** `<undo_root>/changesets/<id>/` holds `meta.json` (the
changeset's kind, time, undone flag, and its files' paths and hashes)
and a `blobs/` folder of pre-image bytes, one file per touched file
that had one. A changeset is a self-contained directory: deleting it
(TTL, or `/v1/purge`) removes its own blobs with it, so there is never
a blob shared between changesets to reference-count.

**A file entry's shape**, `FileEntry(path, pre_image, written_sha256)`:

- `pre_image` is the file's bytes *before* this write, or `None` if
  the write created it (there was nothing before).
- `written_sha256` is the hash of what the file *is* after this write,
  or `None` if this write's effect was to leave the path absent (the
  old side of a rename).

Undo restores exactly what those two fields describe, and nothing
else: given a path that still hashes to `written_sha256` (or, when
that is `None`, a path that is still absent), it either writes back
`pre_image` or deletes the file. **There is no argument through which
new content reaches this module.** A file that has changed since is
refused and left untouched -- vaultd's ordinary compare-and-swap,
applied to the undo direction too.

**Caps** (changesets per hour, files per changeset, undos per hour,
and rev. 3's folder and move budgets) are vaultd's own copy of the
write plan's section 6.4 limits; the bot keeps another. Their values
come from `self.limits` (limits.py, `<root>/limits.json`), which the
user tunes via the bot; config.py holds the defaults. `precheck` is called before any byte touches disk, so a
refusal here never needs an undo of its own.

**Two writers** (lens L4, the L4 spec section 5). `meta.writer` is
`claude` (the default: every changeset written before L4 has none) or
`echo` (Echo's inbox writer, echo.py). It filters every counter: the
Claude prechecks (`precheck`, `precheck_moves`, `precheck_folders`)
and their `count_recent`/`_recent_sum` count Claude's changesets only,
so Echo's writes never spend Claude's budget, and Echo's own caps
(config.py's `ECHO_*` constants, `precheck_echo`, the undo route) count
Echo's only and ignore the user's tunable limits and counter reset:
Claude's caps, even at 0, never block Echo. Joining another writer's
changeset is refused (`changeset_writer_mismatch`), and so is undoing
one with the wrong `?writer=` (api.py), in both directions.
`GET /v1/changes` carries each changeset's writer.

**The clock is injectable** (`clock`, default `datetime.now(UTC)`),
the same shape as `Supervisor`'s `monotonic` -- tests freeze it to
exercise the 14-day TTL and the per-hour caps without a real sleep.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from vaultd.config import (
    ECHO_CHANGESETS_PER_DAY,
    ECHO_FILES_PER_CHANGESET,
    UNDO_TTL_DAYS,
)
from vaultd.limits import LimitsStore

Clock = Callable[[], datetime]

# Lens L4: who wrote a changeset (module docstring). A meta without the
# key predates L4, and is Claude's.
CLAUDE = "claude"
ECHO = "echo"
WRITERS = (CLAUDE, ECHO)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# The closed set of reason codes a knowledge-route refusal can carry into
# the operator log (see api.py's `_log_refused`). The HTTP response never
# sees these -- the client keeps the one bare 403/404 -- but the log line
# says which rule fired. Defined here, not in knowledge.py, so both
# `knowledge.Refused` and `CapExceeded` (this module) can require one
# without an import cycle (knowledge.py already imports this module).
REFUSAL_REASONS = frozenset(
    {
        # knowledge.py: path shape (write-plan section 4.1)
        "not_md",
        "dot_segment",
        "anchor_path",
        "symlink",
        # knowledge.py: content
        "bad_utf8",
        "too_large",
        "bad_frontmatter",
        "reclassify",
        # knowledge.py: classification (settings.md, folder rules, section 4.2/4.4)
        "settings_invalid",
        "not_knowledge",
        "folder_not_knowledge",
        "content_not_knowledge",
        # knowledge.py: create/update placement
        "name_taken",
        "folder_missing",
        # knowledge.py: rename (section 4, backlinks)
        "same_path",
        "dest_taken",
        "dest_not_knowledge",
        "linked_from_non_knowledge",
        "ambiguous_basename",
        # knowledge.py: folder auto-creation (rev. 3, plan section 14)
        "folder_not_under_knowledge",
        "folder_too_deep",
        "folder_name_bad",
        # undo.py: caps (section 6.4) and undo-of-undo/unknown ids
        "cap_files",
        "cap_changesets",
        "cap_undos",
        "changeset_kind_mismatch",
        "undo_of_undo",
        # undo.py: rev. 3's own caps -- folders created, files moved
        "cap_folders",
        "cap_folders_day",
        "cap_moves",
        "cap_moves_day",
        # api.py: GET /v1/knowledge, file simply absent
        "missing",
        # Lens L4 (the L4 spec section 5): Echo's inbox writer (echo.py)
        # and the writer split in this module.
        "inbox_unavailable",
        "bad_name",
        "frontmatter_keys",
        "changeset_writer_mismatch",
        "cap_echo",
    }
)


class CapExceeded(Exception):
    """A vaultd-side cap (files/changeset, changesets/hour, undos/hour) was hit.

    `reason` is required and must be one of `REFUSAL_REASONS`, so the
    operator log always has something to say even though the HTTP
    response stays a bare 403 (write-plan section 4, 6.2).
    """

    def __init__(self, reason: str) -> None:
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"unknown refusal reason: {reason!r}")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FileEntry:
    path: str
    pre_image: bytes | None
    written_sha256: str | None


@dataclass(frozen=True)
class ChangeSummary:
    id: str
    kind: str
    time: str
    undone: bool
    files: list[dict] = field(default_factory=list)
    writer: str = CLAUDE

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "time": self.time,
            "undone": self.undone,
            "files": self.files,
            "writer": self.writer,
        }


def _writer(meta: dict) -> str:
    return meta.get("writer", CLAUDE)


class UndoStore:
    def __init__(self, root: Path, *, clock: Clock = utc_now, ttl_days: int = UNDO_TTL_DAYS) -> None:
        self.root = Path(root)
        self._clock = clock
        self._ttl = timedelta(days=ttl_days)
        (self.root / "changesets").mkdir(parents=True, exist_ok=True)
        # Beside `changesets/`, never inside it: `purge` rebuilds that
        # directory, and a changeset id can never be `limits.json`.
        self.limits = LimitsStore(self.root / "limits.json")

    # -- layout ----------------------------------------------------------

    def _base(self) -> Path:
        return self.root / "changesets"

    def _dir(self, changeset_id: str) -> Path:
        return self._base() / changeset_id

    def _meta_path(self, changeset_id: str) -> Path:
        return self._dir(changeset_id) / "meta.json"

    # -- TTL ---------------------------------------------------------------

    def sweep(self) -> None:
        """Delete every changeset older than the TTL. Never reads a blob."""
        cutoff = self._clock() - self._ttl
        try:
            entries = list(os.scandir(self._base()))
        except OSError:
            return
        for entry in entries:
            meta = self._read_meta(entry.name)
            if meta is None:
                continue
            try:
                when = datetime.fromisoformat(meta["time"])
            except (KeyError, ValueError):
                continue
            if when < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)

    # -- reading -----------------------------------------------------------

    def _read_meta(self, changeset_id: str) -> dict | None:
        try:
            return json.loads(self._meta_path(changeset_id).read_text())
        except (OSError, ValueError):
            return None

    def _write_meta(self, changeset_id: str, meta: dict) -> None:
        self._meta_path(changeset_id).write_text(json.dumps(meta))

    def exists(self, changeset_id: str) -> bool:
        self.sweep()
        return self._read_meta(changeset_id) is not None

    def kind_of(self, changeset_id: str) -> str | None:
        self.sweep()
        meta = self._read_meta(changeset_id)
        return meta["kind"] if meta else None

    def writer_of(self, changeset_id: str) -> str | None:
        """`claude` or `echo`, or None for an unknown (or expired) id."""
        self.sweep()
        meta = self._read_meta(changeset_id)
        return _writer(meta) if meta else None

    def files_of(self, changeset_id: str) -> list[FileEntry]:
        """The stored FileEntry list for `changeset_id`, pre-images included."""
        self.sweep()
        meta = self._read_meta(changeset_id)
        if meta is None:
            return []
        cdir = self._dir(changeset_id)
        out = []
        for f in meta["files"]:
            pre_image = None
            if f.get("pre_image_file"):
                pre_image = (cdir / "blobs" / f["pre_image_file"]).read_bytes()
            out.append(FileEntry(f["path"], pre_image, f.get("written_sha256")))
        return out

    def list_changes(self) -> list[dict]:
        """The index for `GET /v1/changes`: no pre-image bytes, ever."""
        self.sweep()
        try:
            entries = sorted(os.scandir(self._base()), key=lambda e: e.name)
        except OSError:
            entries = []
        out = []
        for entry in entries:
            meta = self._read_meta(entry.name)
            if meta is None:
                continue
            out.append(
                ChangeSummary(
                    id=meta["id"],
                    kind=meta["kind"],
                    time=meta["time"],
                    undone=bool(meta.get("undone", False)),
                    files=[{"path": f["path"], "sha256": f.get("written_sha256")} for f in meta["files"]],
                    writer=_writer(meta),
                ).as_json()
            )
        out.sort(key=lambda m: m["time"])
        return out

    def _since(self, window: timedelta, writer: str = CLAUDE) -> datetime:
        """The start of a counting window: `window` ago, or the user's
        last counter reset if that is later (limits.py). The reset is
        Claude's: Echo's windows ignore it (module docstring)."""
        cutoff = self._clock() - window
        if writer != CLAUDE:
            return cutoff
        reset_at = self.limits.get().counters_reset_at
        if reset_at:
            cutoff = max(cutoff, datetime.fromtimestamp(reset_at, tz=timezone.utc))
        return cutoff

    def count_recent(self, kind: str, writer: str = CLAUDE, *, hours: int = 1) -> int:
        """How many `kind` changesets of `writer` started within the last
        `hours` (since the last counter reset, if later and the writer
        is Claude)."""
        self.sweep()
        cutoff = self._since(timedelta(hours=hours), writer)
        count = 0
        for meta_dict in self.list_changes():
            if meta_dict["kind"] != kind or meta_dict["writer"] != writer:
                continue
            if datetime.fromisoformat(meta_dict["time"]) >= cutoff:
                count += 1
        return count

    def _recent_sum(self, field: str) -> int:
        """Sum of `field` across every Claude 'write' changeset started within
        the last 24h (rolling, not calendar-day) -- MOVES_PER_DAY's and
        FOLDERS_PER_DAY's own counters (rev. 3). `field` is either an
        int counter (`move_files`) or a list whose length is the count
        (`folders`); reads raw meta directly, never `list_changes`
        (which drops exactly the fields this needs). Since the last
        counter reset, if later."""
        self.sweep()
        cutoff = self._since(timedelta(hours=24))
        total = 0
        try:
            scanned = list(os.scandir(self._base()))
        except OSError:
            scanned = []
        for entry in scanned:
            meta = self._read_meta(entry.name)
            if meta is None or meta.get("kind") != "write" or _writer(meta) != CLAUDE:
                continue
            try:
                when = datetime.fromisoformat(meta["time"])
            except (KeyError, ValueError):
                continue
            if when < cutoff:
                continue
            value = meta.get(field, [] if field == "folders" else 0)
            total += len(value) if isinstance(value, list) else value
        return total

    # -- capacity, checked before any byte is written -----------------------

    def precheck(self, changeset_id: str, kind: str, n_files: int) -> None:
        """Raise CapExceeded if adding `n_files` content-write files to
        `changeset_id` would not fit (FILES_PER_CHANGESET). A rename's
        own files (`precheck_moves`) are counted separately -- a
        changeset's `files` list can hold both kinds (a content write
        and a rename can share one 10-minute idle window), so this
        reads the `content_files` counter, never `len(meta["files"])`.

        Pure read: never creates the changeset or writes a blob, so a
        cap refusal here leaves nothing to roll back. Claude's only: an
        Echo changeset is refused whole (`changeset_writer_mismatch`).
        """
        self.sweep()
        limits = self.limits.get()
        meta = self._read_meta(changeset_id)
        if meta is None:
            if kind == "write" and self.count_recent("write") >= limits.changesets_per_hour:
                raise CapExceeded("cap_changesets")
            current_files = 0
        else:
            if _writer(meta) != CLAUDE:
                raise CapExceeded("changeset_writer_mismatch")
            if meta["kind"] != kind:
                raise CapExceeded("changeset_kind_mismatch")
            current_files = meta.get("content_files", 0)
        if current_files + n_files > limits.files_per_changeset:
            raise CapExceeded("cap_files")

    def precheck_moves(self, changeset_id: str, n_files: int) -> None:
        """Rev. 3's move budget: MOVE_FILES_PER_CHANGESET per changeset,
        MOVES_PER_DAY per rolling 24h -- entirely separate from
        `precheck` above, so a rename's moved file and its rewritten
        backlinks never spend the content-write budget, or vice versa.
        Mints/rate-limits a new changeset exactly like `precheck` does
        (CHANGESETS_PER_HOUR, kind mismatch), so a caller doing both a
        content write and a rename in the same changeset may call
        either precheck first."""
        self.sweep()
        limits = self.limits.get()
        meta = self._read_meta(changeset_id)
        if meta is None:
            if self.count_recent("write") >= limits.changesets_per_hour:
                raise CapExceeded("cap_changesets")
            current = 0
        else:
            if _writer(meta) != CLAUDE:
                raise CapExceeded("changeset_writer_mismatch")
            if meta["kind"] != "write":
                raise CapExceeded("changeset_kind_mismatch")
            current = meta.get("move_files", 0)
        if current + n_files > limits.move_files_per_changeset:
            raise CapExceeded("cap_moves")
        if self._recent_sum("move_files") + n_files > limits.moves_per_day:
            raise CapExceeded("cap_moves_day")

    def precheck_folders(self, changeset_id: str, n_folders: int) -> None:
        """Rev. 3's folder-creation budget: FOLDERS_PER_CHANGESET per
        changeset, FOLDERS_PER_DAY per rolling 24h. Always called
        alongside `precheck`/`precheck_moves` for the same write (a
        folder is only ever created on the way to a content write or a
        rename), so it mints no changeset of its own."""
        self.sweep()
        limits = self.limits.get()
        meta = self._read_meta(changeset_id)
        if meta is not None and _writer(meta) != CLAUDE:
            raise CapExceeded("changeset_writer_mismatch")
        current = len(meta.get("folders", [])) if meta else 0
        if current + n_folders > limits.folders_per_changeset:
            raise CapExceeded("cap_folders")
        if self._recent_sum("folders") + n_folders > limits.folders_per_day:
            raise CapExceeded("cap_folders_day")

    def precheck_echo(self, changeset_id: str) -> None:
        """Echo's caps (lens L4), before any byte is written: a new
        changeset needs room under ECHO_CHANGESETS_PER_DAY (rolling 24h,
        Echo's changesets only, no counter reset), an existing one must
        be Echo's and hold fewer than ECHO_FILES_PER_CHANGESET files.
        Claude's limits are never read. A replay of a changeset that
        already holds its note never reaches here (`echo_replay`)."""
        self.sweep()
        meta = self._read_meta(changeset_id)
        if meta is None:
            if self.count_recent("write", ECHO, hours=24) >= ECHO_CHANGESETS_PER_DAY:
                raise CapExceeded("cap_echo")
            return
        if _writer(meta) != ECHO:
            raise CapExceeded("changeset_writer_mismatch")
        if meta.get("content_files", 0) + 1 > ECHO_FILES_PER_CHANGESET:
            raise CapExceeded("cap_echo")

    def echo_replay(self, changeset_id: str) -> tuple[str, str] | None:
        """(path, sha256) of the note an Echo changeset already created,
        for a replayed `PUT /v1/echo/inbox` (the bot retries a write
        whose answer it never got, with the same changeset id); None
        for a new changeset. Another writer's id is refused."""
        self.sweep()
        meta = self._read_meta(changeset_id)
        if meta is None:
            return None
        if _writer(meta) != ECHO:
            raise CapExceeded("changeset_writer_mismatch")
        for entry in meta["files"]:
            if entry.get("written_sha256"):
                return entry["path"], entry["written_sha256"]
        return None

    # -- writing -------------------------------------------------------------

    def _new_meta(self, changeset_id: str, kind: str, writer: str) -> dict:
        if writer not in WRITERS:
            raise ValueError(f"unknown writer: {writer!r}")
        return {
            "id": changeset_id,
            "kind": kind,
            "time": _iso(self._clock()),
            "undone": False,
            "files": [],
            "writer": writer,
        }

    def _append_files(
        self,
        changeset_id: str,
        kind: str,
        entries: list[FileEntry],
        counter: str,
        count: int,
        writer: str = CLAUDE,
    ) -> None:
        cdir = self._dir(changeset_id)
        blobs_dir = cdir / "blobs"
        blobs_dir.mkdir(parents=True, exist_ok=True)
        meta = self._read_meta(changeset_id)
        if meta is None:
            meta = self._new_meta(changeset_id, kind, writer)
        next_index = sum(1 for _ in blobs_dir.iterdir())
        for entry in entries:
            pre_image_file = None
            if entry.pre_image is not None:
                pre_image_file = f"{next_index}.blob"
                (blobs_dir / pre_image_file).write_bytes(entry.pre_image)
                next_index += 1
            meta["files"].append(
                {"path": entry.path, "pre_image_file": pre_image_file, "written_sha256": entry.written_sha256}
            )
        meta[counter] = meta.get(counter, 0) + count
        self._write_meta(changeset_id, meta)

    def append(self, changeset_id: str, kind: str, entries: list[FileEntry], *, writer: str = CLAUDE) -> None:
        """Add `entries` to `changeset_id` as content-write files,
        creating it (with `kind`) if new. Callers must have called
        `precheck` for the same counts first, under the same lock --
        this does not re-check caps, only writes. A content write's
        `entries` is always one FileEntry per file, so the counter is
        `len(entries)`. `writer` stamps a new changeset (an existing
        one keeps its own; the prechecks refused a mismatch)."""
        self._append_files(changeset_id, kind, entries, "content_files", len(entries), writer)

    def append_move(self, changeset_id: str, kind: str, entries: list[FileEntry], n_files: int) -> None:
        """Same as `append`, but for a rename's moved file and its
        rewritten backlinks (rev. 3): counted under `move_files`, never
        `content_files`, so the two budgets stay independent. Callers
        must have called `precheck_moves` for the same `n_files` first.

        `n_files` is passed explicitly, not derived from `len(entries)`:
        an ordinary rename's `entries` holds two undo records (new path,
        now-absent old path) for the one moved file, plus one per
        rewritten backlink -- `len(entries) - 1` -- but a mid-rename
        failure (`MidRenameFailure`) records only the new path's single
        entry, still one moved file, and the caller knows which shape
        it has."""
        self._append_files(changeset_id, kind, entries, "move_files", n_files)

    def append_folders(
        self, changeset_id: str, kind: str, folders: list[str], *, writer: str = CLAUDE
    ) -> None:
        """Record folders created for `changeset_id` (rev. 3) -- for the
        per-changeset/per-day cap, and so undo can remove them again
        (deepest first, only if still empty). Callers must have called
        `precheck_folders` for the same count first."""
        cdir = self._dir(changeset_id)
        cdir.mkdir(parents=True, exist_ok=True)
        meta = self._read_meta(changeset_id)
        if meta is None:
            meta = self._new_meta(changeset_id, kind, writer)
        meta.setdefault("folders", [])
        meta["folders"].extend(folders)
        self._write_meta(changeset_id, meta)

    def folders_of(self, changeset_id: str) -> list[str]:
        """The folder paths created for `changeset_id`, shallow to deep
        (creation order) -- undo removes them in reverse."""
        self.sweep()
        meta = self._read_meta(changeset_id)
        return list(meta.get("folders", [])) if meta else []

    def mark_undone(self, changeset_id: str) -> None:
        meta = self._read_meta(changeset_id)
        if meta is None:
            return
        meta["undone"] = True
        self._write_meta(changeset_id, meta)

    def new_undo_id(self) -> str:
        return f"undo-{uuid.uuid4().hex}"

    def purge(self) -> None:
        shutil.rmtree(self._base(), ignore_errors=True)
        self._base().mkdir(parents=True, exist_ok=True)
        self.limits.reset()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()
