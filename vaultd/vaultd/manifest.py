"""The manifest: what the bot may know exists (plan section 5.4, `GET /v1/manifest`).

Two scopes, and nothing else is listed:

- `anchor` -- every `.md` directly inside `Anchor/Memory/` or
  `Anchor/Journal/` (the writable set, see paths.py);
- `note` -- every other `.md` whose frontmatter says `anchor: read`
  and whose size is at most NOTE_MAX_BYTES.

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
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from vaultd import frontmatter, paths
from vaultd.config import NOTE_MAX_BYTES

_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


@dataclass(frozen=True)
class Entry:
    path: str
    sha256: str
    size: int
    scope: str

    def as_json(self) -> dict:
        return {"path": self.path, "sha256": self.sha256, "size": self.size, "scope": self.scope}


@dataclass(frozen=True)
class _Cached:
    key: tuple[int, int, int, int]
    sha256: str
    opted_in: bool


class Manifest:
    def __init__(self, root: Path, *, note_max_bytes: int = NOTE_MAX_BYTES) -> None:
        self.root = root
        self.note_max_bytes = note_max_bytes
        self._cache: dict[str, _Cached] = {}
        # How many files were actually read on the last scan; tests use
        # it to prove the cache hits and misses when it should.
        self.last_reads = 0

    def scan(self) -> list[Entry]:
        entries: list[Entry] = []
        seen: set[str] = set()
        self.last_reads = 0
        self._walk(self.root, "", entries, seen)
        for gone in set(self._cache) - seen:
            del self._cache[gone]
        entries.sort(key=lambda e: e.path)
        return entries

    def _walk(self, directory: Path, prefix: str, entries: list[Entry], seen: set[str]) -> None:
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
                    self._walk(Path(entry.path), rel + "/", entries, seen)
                    continue
                if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".md"):
                    continue
            except OSError:
                continue
            listed = self._consider(Path(entry.path), rel)
            if listed is not None:
                seen.add(rel)
                entries.append(listed)

    def _consider(self, full: Path, rel: str) -> Entry | None:
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
                    opted_in=(not anchor_scope) and frontmatter.is_opted_in(data),
                )
                self._cache[rel] = cached
        finally:
            os.close(fd)
        if anchor_scope:
            return Entry(rel, cached.sha256, key[1], "anchor")
        if cached.opted_in:
            return Entry(rel, cached.sha256, key[1], "note")
        return None


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
