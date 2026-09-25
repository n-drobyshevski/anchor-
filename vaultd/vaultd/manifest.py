"""The manifest: what the bot may know exists (plan section 5.4, `GET /v1/manifest`).

Two scopes, and nothing else is listed:

- `anchor` -- every `.md` directly inside `Anchor/Memory/` or
  `Anchor/Journal/` (the writable set, see paths.py);
- `note` -- every other `.md` of at most NOTE_MAX_BYTES whose effective
  class (classes.py) is `personal` or `knowledge`. The entry carries
  that class. `Anchor/settings.md` is never listed.

**Invisible notes contribute counts only, never a path** (8e plan
section 4): the `summary` says how many notes disagreed with their
folder, still say `anchor: read`, or carry a value Anchor does not
know, and whether the settings file is usable.

**Only the mark is cached per file; the class is not.** The rules are
read once per scan and every class is recomputed from the cached mark,
so an edit to `Anchor/settings.md` reclassifies every note on the next
scan without re-reading one of them. That is also why every note that
was considered stays in the cache, listed or not.

Dot-folders and dot-files are skipped, and a symlink is never followed
or listed, so `.obsidian/`, `.trash/` and a link pointing out of the
vault are invisible here exactly as they are to `GET /v1/file`.

**The cache key is `(st_ino, st_size, st_mtime_ns, st_ctime_ns)` per
path, not the mtime alone.** `ob` sets file mtimes from the server, so
a rewrite by Sync can land with the same size and the same mtime as
before. Replacing a file (a new inode) or writing to it (a new ctime,
which no one can set) still changes the key. The key is read from the
open file's `fstat`, so it describes the bytes actually hashed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from vaultd import classes, frontmatter, paths
from vaultd.config import NOTE_MAX_BYTES
from vaultd.frontmatter import NoteMark

logger = logging.getLogger("vaultd.manifest")

_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


@dataclass(frozen=True)
class Entry:
    path: str
    sha256: str
    size: int
    scope: str
    note_class: str | None = None

    def as_json(self) -> dict:
        out = {"path": self.path, "sha256": self.sha256, "size": self.size, "scope": self.scope}
        if self.scope == "note":
            out["class"] = self.note_class
        return out


@dataclass
class Summary:
    """Counts over the notes considered, and the settings file's state. No paths."""

    conflict: int = 0
    legacy_read: int = 0
    unknown_value: int = 0
    settings: str = "absent"

    def as_json(self) -> dict:
        return {
            "conflict": self.conflict,
            "legacy_read": self.legacy_read,
            "unknown_value": self.unknown_value,
            "settings": self.settings,
        }


@dataclass(frozen=True)
class Scan:
    entries: list[Entry]
    summary: Summary


@dataclass(frozen=True)
class _Cached:
    key: tuple[int, int, int, int]
    sha256: str
    # None for Anchor's own files, whose frontmatter is never consulted.
    mark: NoteMark | None


class Manifest:
    def __init__(self, root: Path, *, note_max_bytes: int = NOTE_MAX_BYTES) -> None:
        self.root = root
        self.note_max_bytes = note_max_bytes
        self._cache: dict[str, _Cached] = {}
        # How many files were actually read on the last scan; tests use
        # it to prove the cache hits and misses when it should.
        self.last_reads = 0
        self._settings_state: str | None = None

    def scan(self) -> Scan:
        rules = classes.load_rules(self.root)
        self._note_settings_state(rules.state)
        entries: list[Entry] = []
        seen: set[str] = set()
        summary = Summary(settings=rules.state)
        self.last_reads = 0
        self._walk(self.root, "", entries, seen, rules, summary)
        for gone in set(self._cache) - seen:
            del self._cache[gone]
        entries.sort(key=lambda e: e.path)
        return Scan(entries, summary)

    def _note_settings_state(self, state: str) -> None:
        """Log a change of the settings file's state: the state only."""
        if state != self._settings_state:
            if state == "invalid":
                logger.warning("settings unusable; no note is listed", extra={"event": "settings_invalid"})
            elif self._settings_state is not None:
                logger.info("settings usable", extra={"event": f"settings_{state}"})
            self._settings_state = state

    def _walk(
        self,
        directory: Path,
        prefix: str,
        entries: list[Entry],
        seen: set[str],
        rules: classes.FolderRules,
        summary: Summary,
    ) -> None:
        try:
            listing = list(os.scandir(directory))
        except OSError:
            return
        for entry in listing:
            if entry.name.startswith("."):
                continue
            rel = prefix + entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    self._walk(Path(entry.path), rel + "/", entries, seen, rules, summary)
                    continue
                if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".md"):
                    continue
            except OSError:
                continue
            if classes.is_settings_file(rel):
                continue
            cached = self._consider(Path(entry.path), rel)
            if cached is None:
                continue
            seen.add(rel)
            listed = _entry(rel, cached, rules, summary)
            if listed is not None:
                entries.append(listed)

    def _consider(self, full: Path, rel: str) -> _Cached | None:
        """The cached hash and mark for a file, reading it only if it changed."""
        anchor_scope = paths.is_writable(rel)
        try:
            fd = os.open(full, _FILE_FLAGS)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None
            if not anchor_scope and st.st_size > self.note_max_bytes:
                return None
            key = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            cached = self._cache.get(rel)
            if cached is None or cached.key != key:
                data = _read_all(fd)
                self.last_reads += 1
                cached = _Cached(
                    key=key,
                    sha256=hashlib.sha256(data).hexdigest(),
                    mark=None if anchor_scope else frontmatter.note_mark(data),
                )
                self._cache[rel] = cached
        finally:
            os.close(fd)
        return cached


def _entry(rel: str, cached: _Cached, rules: classes.FolderRules, summary: Summary) -> Entry | None:
    size = cached.key[1]
    if cached.mark is None:
        return Entry(rel, cached.sha256, size, "anchor")
    if rules.state == "invalid":
        return None
    resolved = classes.effective_class(rel, cached.mark, rules)
    summary.conflict += resolved.conflict
    summary.legacy_read += resolved.legacy_read
    summary.unknown_value += resolved.unknown_value
    if resolved.note_class is None:
        return None
    return Entry(rel, cached.sha256, size, "note", resolved.note_class)


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
