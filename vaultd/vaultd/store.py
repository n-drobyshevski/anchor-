"""Writes: compare-and-swap, or nothing (plan section 5.4).

Every write and delete names the content it expects to replace, by
hash, and is refused with 412 if the file on disk is not that content.
This is what lets the bot promise "Anchor never overwrites a file whose
current content it has not ingested": a user's edit that arrived after
the bot's last manifest makes the hash stale, and the write fails
instead of clobbering it.

**Create-only** (`if_sha256` null) writes and fsyncs a temp file, then
`os.link`s it onto the target. `link` fails atomically with EEXIST if
the target exists -- a check-then-write would leave a window in which
`ob` could create the file between the check and the write.

**Update** writes and fsyncs the temp file, re-checks the hash of the
current bytes, then `os.replace`s. A failure at any step before the
replace leaves the old content in place, and the temp file is always
removed.

**The residual race** is recorded in docs/decisions.md rather than
papered over: `ob` is another process, and the lock below does not
cover it. Between the re-check and the replace, microseconds wide, `ob`
can still write. The next pass closes it -- the manifest then shows a
hash the bot has not ingested, and the bot ingests before it renders.

Writes are serialised through one asyncio.Lock (held by the API
layer), so two requests from the bot can never interleave.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path

from vaultd import paths


class Conflict(Exception):
    """412: the file is not the content the caller expected (or exists, for create-only)."""


class Missing(Exception):
    """404: nothing to delete."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Store:
    def __init__(self, vault_path: Path, tmp_path: Path) -> None:
        self.vault_path = vault_path
        self.tmp_path = tmp_path

    # -- writes --------------------------------------------------------

    def put(self, rel: str, data: bytes, if_sha256: str | None) -> str:
        """Create-only or compare-and-swap write to Anchor's own scope. Returns the new hash."""
        if not paths.is_writable(rel):
            raise paths.Refused
        return self.put_unchecked(rel, data, if_sha256)

    def put_unchecked(self, rel: str, data: bytes, if_sha256: str | None) -> str:
        """The same create-only/CAS write, without the Anchor-scope gate.

        For knowledge writes and undo restores, whose caller has already
        decided the path is writable by its own rules (classes.py, or a
        stored pre-image being put back exactly where it came from).
        """
        folder, name = rel.rsplit("/", 1)
        with paths.open_root(self.vault_path) as root_fd, paths.open_root(self.tmp_path) as tmp_fd:
            dir_fd = paths.open_dir(root_fd, folder.split("/"), create=True)
            assert dir_fd is not None
            try:
                if if_sha256 is not None:
                    self._expect(dir_fd, name, if_sha256)
                tmp_name = self._write_temp(tmp_fd, data)
                try:
                    if if_sha256 is None:
                        try:
                            os.link(tmp_name, name, src_dir_fd=tmp_fd, dst_dir_fd=dir_fd)
                        except FileExistsError:
                            raise Conflict from None
                    else:
                        # Re-check against the bytes on disk *now*, after
                        # the slow part, then swap in one rename.
                        self._expect(dir_fd, name, if_sha256)
                        os.replace(tmp_name, name, src_dir_fd=tmp_fd, dst_dir_fd=dir_fd)
                    os.fsync(dir_fd)
                finally:
                    _unlink_quietly(tmp_name, tmp_fd)
            finally:
                os.close(dir_fd)
        return sha256(data)

    def delete(self, rel: str, if_sha256: str) -> None:
        if not paths.is_writable(rel):
            raise paths.Refused
        self.delete_unchecked(rel, if_sha256)

    def delete_unchecked(self, rel: str, if_sha256: str) -> None:
        """The same compare-and-swap delete, without the Anchor-scope gate."""
        folder, name = rel.rsplit("/", 1)
        with paths.open_root(self.vault_path) as root_fd:
            dir_fd = paths.open_dir(root_fd, folder.split("/"))
            if dir_fd is None:
                raise Missing
            try:
                current = paths.read_regular_at(dir_fd, name)
                if current is None:
                    raise Missing
                if sha256(current) != if_sha256:
                    raise Conflict
                os.unlink(name, dir_fd=dir_fd)
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def purge(self) -> int:
        """Delete every regular `.md` directly inside Anchor's two folders.

        Idempotent. Skips dot-files, subfolders and symlinks: none of
        those is a file Anchor wrote, and the purge must not be the one
        route that follows a link.
        """
        count = 0
        with paths.open_root(self.vault_path) as root_fd:
            for folder in paths.ANCHOR_DIRS:
                dir_fd = paths.open_dir(root_fd, folder.split("/"))
                if dir_fd is None:
                    continue
                try:
                    for name in os.listdir(dir_fd):
                        if name.startswith(".") or not name.endswith(".md"):
                            continue
                        try:
                            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        try:
                            os.unlink(name, dir_fd=dir_fd)
                        except FileNotFoundError:
                            continue
                        count += 1
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        return count

    # -- helpers -------------------------------------------------------

    def _expect(self, dir_fd: int, name: str, if_sha256: str) -> None:
        current = paths.read_regular_at(dir_fd, name)
        if current is None or sha256(current) != if_sha256:
            raise Conflict

    def _write_temp(self, tmp_fd: int, data: bytes) -> str:
        tmp_name = f"vaultd-{uuid.uuid4().hex}.tmp"
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o644,
            dir_fd=tmp_fd,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            _unlink_quietly(tmp_name, tmp_fd)
            raise
        os.close(fd)
        return tmp_name


def _unlink_quietly(name: str, dir_fd: int) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
