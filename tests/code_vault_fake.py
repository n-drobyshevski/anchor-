"""A stand-in for vaultd's code routes (anchor-code-window-plan.md section 3), for
the bot's CW1 tests.

Not vaultd: it only answers in the shapes `VaultClient.code_tree` /
`code_file` return, refuses what vaultd refuses (`Anchor/` on the first
segment in any case, a path outside the roots), and records every call
so a test can assert what the bot asked -- and what it never asked.
Synthetic notes only.
"""

from __future__ import annotations

import hashlib

from app.vault import code_access
from app.vault.client import CodeFile, CodeTree, CodeTreeEntry, ServiceStatus
from app.vault.errors import VaultError

NOTES = {
    "Notes/plan.md": ("personal", "Синтетический план."),
    "Notes/sub/deep.md": ("knowledge", "Глубоко."),
    "Notes2/other.md": ("none", "Соседняя папка."),
    "Lens/Ashby.md": ("lens", "Requisite variety."),
    "Projects/Anchor/x.md": ("none", "Вложенная папка Anchor."),
}


class FakeCodeVault:
    def __init__(self, notes: dict[str, tuple[str, str]] | None = None) -> None:
        self.notes = dict(NOTES if notes is None else notes)
        self.code_access = True
        # A vault error code every code call raises (`not_found` for the
        # switch off, `conflict` for settings_unusable, ...).
        self.error: str | None = None
        self.status_error: str | None = None
        self.truncate_at: int | None = None
        self.calls: list[tuple] = []

    async def status(self) -> ServiceStatus:
        self.calls.append(("status",))
        if self.status_error:
            raise VaultError(self.status_error)
        return ServiceStatus(
            sync_running=True, restarts=0, last_exit_code=None, running_since=None,
            code_access=self.code_access,
        )

    def _served(self, roots: list[str], path: str) -> bool:
        return path in self.notes and code_access.covers(roots, path)

    async def code_tree(self, roots: list[str]) -> CodeTree:
        self.calls.append(("tree", list(roots)))
        if self.error:
            raise VaultError(self.error)
        entries = [
            CodeTreeEntry(path=p, note_class=c, bytes=len(t.encode()), mtime="2026-11-16T10:00:00Z")
            for p, (c, t) in sorted(self.notes.items())
            if self._served(roots, p)
        ]
        truncated = False
        if self.truncate_at is not None and len(entries) > self.truncate_at:
            entries, truncated = entries[: self.truncate_at], True
        return CodeTree(files=entries, truncated=truncated)

    async def code_file(self, path: str, roots: list[str]) -> CodeFile:
        self.calls.append(("file", path, list(roots)))
        if self.error:
            raise VaultError(self.error)
        if not self._served(roots, path):
            raise VaultError("not_found")
        note_class, text = self.notes[path]
        return CodeFile(
            path=path, note_class=note_class,
            sha256=hashlib.sha256(text.encode()).hexdigest(), text=text,
        )

    def factory(self, settings=None) -> FakeCodeVault:
        return self
