"""An in-memory vault with vaultd's semantics, for the sync pass's tests.

Not vaultd: the bot's tests import nothing from it. This fake answers
the same questions the same way -- create-only fails if the file
exists, an update or delete with a stale hash is a conflict, only
Anchor's two folders are writable -- and lets a test reach in between
calls to play the user editing a file on their phone.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from typing import Callable

from app.vault import errors
from app.vault.client import FileContent, Manifest, ManifestEntry, NotesSummary, ServiceStatus
from app.vault.errors import VaultError

WRITABLE = re.compile(r"^Anchor/(Memory|Journal)/[^/]+\.md$")


def sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class FakeVault:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.running = True
        self.down = False
        self.purged = 0
        # 8e: notes vaultd would list, path -> (class, content), and the
        # summary it would report. Synthetic; never a real vault.
        self.notes: dict[str, tuple[str, str]] = {}
        self.summary = NotesSummary(conflict=0, legacy_read=0, unknown_value=0, settings="absent")
        # Called with (path) just before a PUT lands: a test's chance to
        # "edit the file on the phone" after the manifest was read.
        self.before_put: Callable[[str], None] | None = None
        # Raise this (once) instead of performing / after performing a PUT.
        self.crash_before_put: Exception | None = None
        self.crash_after_put: Exception | None = None

    # The factory the sync pass takes.
    def __call__(self, settings) -> "FakeVault":
        return self

    def _check(self) -> None:
        if self.down:
            raise VaultError(errors.UNAVAILABLE)

    async def status(self) -> ServiceStatus:
        self.calls.append(("status", ""))
        self._check()
        return ServiceStatus(
            sync_running=self.running,
            restarts=0,
            last_exit_code=None,
            running_since=datetime.datetime(2026, 9, 25, 8, 0, tzinfo=datetime.timezone.utc),
        )

    async def manifest(self) -> Manifest:
        self.calls.append(("manifest", ""))
        self._check()
        entries = [
            ManifestEntry(path, sha(content), len(content.encode()), "anchor")
            for path, content in sorted(self.files.items())
            if WRITABLE.match(path)
        ]
        entries += [
            ManifestEntry(path, sha(content), len(content.encode()), "note", note_class)
            for path, (note_class, content) in sorted(self.notes.items())
        ]
        return Manifest(entries, self.summary)

    async def get_file(self, path: str) -> FileContent:
        self.calls.append(("get", path))
        self._check()
        if path not in self.files:
            raise VaultError(errors.NOT_FOUND)
        return FileContent(path, sha(self.files[path]), self.files[path])

    async def put_file(self, path: str, content: str, if_sha256: str | None) -> str:
        self.calls.append(("put", path))
        self._check()
        if not WRITABLE.match(path):
            raise VaultError(errors.REFUSED)
        if self.before_put is not None:
            self.before_put(path)
        if self.crash_before_put is not None:
            exc, self.crash_before_put = self.crash_before_put, None
            raise exc
        current = self.files.get(path)
        if if_sha256 is None:
            if current is not None:
                raise VaultError(errors.CONFLICT)
        elif current is None or sha(current) != if_sha256:
            raise VaultError(errors.CONFLICT)
        self.files[path] = content
        if self.crash_after_put is not None:
            exc, self.crash_after_put = self.crash_after_put, None
            raise exc
        return sha(content)

    async def delete_file(self, path: str, if_sha256: str) -> None:
        self.calls.append(("delete", path))
        self._check()
        if not WRITABLE.match(path):
            raise VaultError(errors.REFUSED)
        current = self.files.get(path)
        if current is None:
            raise VaultError(errors.NOT_FOUND)
        if sha(current) != if_sha256:
            raise VaultError(errors.CONFLICT)
        del self.files[path]

    async def purge(self) -> int:
        self.calls.append(("purge", ""))
        self._check()
        doomed = [p for p in self.files if WRITABLE.match(p)]
        for path in doomed:
            del self.files[path]
        self.purged += len(doomed)
        return len(doomed)

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] in ("put", "delete", "purge")]
