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

**Caps** (`CHANGESETS_PER_HOUR`, `FILES_PER_CHANGESET`, `UNDOS_PER_HOUR`)
are vaultd's own copy of the write plan's section 6.4 limits; the bot
keeps another. `precheck` is called before any byte touches disk, so a
refusal here never needs an undo of its own.

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

from vaultd.config import CHANGESETS_PER_HOUR, FILES_PER_CHANGESET, UNDO_TTL_DAYS

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CapExceeded(Exception):
    """A vaultd-side cap (files/changeset, changesets/hour, undos/hour) was hit."""


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

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "time": self.time,
            "undone": self.undone,
            "files": self.files,
        }


class UndoStore:
    def __init__(self, root: Path, *, clock: Clock = utc_now, ttl_days: int = UNDO_TTL_DAYS) -> None:
        self.root = Path(root)
        self._clock = clock
        self._ttl = timedelta(days=ttl_days)
        (self.root / "changesets").mkdir(parents=True, exist_ok=True)

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
                ).as_json()
            )
        out.sort(key=lambda m: m["time"])
        return out

    def count_recent(self, kind: str) -> int:
        """How many `kind` changesets started within the last hour."""
        self.sweep()
        cutoff = self._clock() - timedelta(hours=1)
        count = 0
        for meta_dict in self.list_changes():
            if meta_dict["kind"] != kind:
                continue
            if datetime.fromisoformat(meta_dict["time"]) >= cutoff:
                count += 1
        return count

    # -- capacity, checked before any byte is written -----------------------

    def precheck(self, changeset_id: str, kind: str, n_files: int) -> None:
        """Raise CapExceeded if adding `n_files` to `changeset_id` would not fit.

        Pure read: never creates the changeset or writes a blob, so a
        cap refusal here leaves nothing to roll back.
        """
        self.sweep()
        meta = self._read_meta(changeset_id)
        if meta is None:
            if kind == "write" and self.count_recent("write") >= CHANGESETS_PER_HOUR:
                raise CapExceeded
            current_files = 0
        else:
            if meta["kind"] != kind:
                raise CapExceeded
            current_files = len(meta["files"])
        if current_files + n_files > FILES_PER_CHANGESET:
            raise CapExceeded

    # -- writing -------------------------------------------------------------

    def append(self, changeset_id: str, kind: str, entries: list[FileEntry]) -> None:
        """Add `entries` to `changeset_id`, creating it (with `kind`) if new.

        Callers must have called `precheck` for the same counts first,
        under the same lock -- this does not re-check caps, only writes.
        """
        cdir = self._dir(changeset_id)
        blobs_dir = cdir / "blobs"
        blobs_dir.mkdir(parents=True, exist_ok=True)
        meta = self._read_meta(changeset_id)
        if meta is None:
            meta = {"id": changeset_id, "kind": kind, "time": _iso(self._clock()), "undone": False, "files": []}
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
        self._write_meta(changeset_id, meta)

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


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()
