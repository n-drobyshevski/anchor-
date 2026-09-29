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
from app.vault.client import (
    FileContent,
    Graph,
    GraphEdge,
    GraphNode,
    Manifest,
    ManifestEntry,
    NotesSummary,
    ServiceStatus,
)
from app.vault.errors import VaultError

WRITABLE = re.compile(r"^Anchor/(Memory|Journal)/[^/]+\.md$")
# L1: `[[target]]`, `[[target|label]]`, `[[target#heading]]` -- enough
# of vaultd's links.py for the fake graph. Embeds are skipped.
WIKILINK = re.compile(r"(?<!!)\[\[([^\]|#]*)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


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
        # 8d: paths that 404 on a direct GET even though the manifest
        # (already read this pass) still lists them -- vaultd
        # recomputes the effective class at read time (8e plan section
        # 4), so a note can become invisible between the two.
        self.missing_notes: set[str] = set()
        # Called with (path) just before a PUT lands: a test's chance to
        # "edit the file on the phone" after the manifest was read.
        self.before_put: Callable[[str], None] | None = None
        # Raise this (once) instead of performing / after performing a PUT.
        self.crash_before_put: Exception | None = None
        self.crash_after_put: Exception | None = None
        # vaultd's copy of Claude's write caps (`/v1/limits`); None
        # means "vaultd's defaults", which the bot compares against its
        # own. `limit_puts` records every PUT body.
        self.limits: dict[str, int] | None = None
        self.limit_puts: list[dict[str, int]] = []
        # L1 (lens plan sections 3-4): a lens note's kind, path -> person
        # or concept (a lens note missing here is a concept), frontmatter
        # summaries the graph reports, a graph to return instead of the
        # one computed from `notes`, and an error to raise from the
        # graph route.
        self.lens_kinds: dict[str, str] = {}
        self.summaries: dict[str, str] = {}
        self.graph: Graph | None = None
        self.graph_error: VaultError | None = None

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

    async def get_limits(self) -> dict[str, int]:
        self._check()
        if self.limits is None:
            from app.core import claude_write_limits

            return {k: claude_write_limits.DEFAULTS.as_dict()[k] for k in claude_write_limits.VAULT_KEYS}
        return dict(self.limits)

    async def put_limits(self, values: dict[str, int]) -> dict[str, int]:
        self._check()
        self.limit_puts.append(dict(values))
        self.limits = dict(values)
        return dict(values)

    async def manifest(self) -> Manifest:
        self.calls.append(("manifest", ""))
        self._check()
        entries = [
            ManifestEntry(path, sha(content), len(content.encode()), "anchor")
            for path, content in sorted(self.files.items())
            if WRITABLE.match(path)
        ]
        entries += [
            ManifestEntry(
                path,
                sha(content),
                len(content.encode()),
                "note",
                note_class,
                self.lens_kinds.get(path, "concept") if note_class == "lens" else None,
            )
            for path, (note_class, content) in sorted(self.notes.items())
        ]
        return Manifest(entries, self.summary)

    async def knowledge_graph(self) -> Graph:
        """vaultd's graph over `notes`: knowledge and lens nodes; a link to
        another of those is `dst`, to a personal note `outside` (nothing
        named), to nothing `unresolved`."""
        self.calls.append(("graph", ""))
        self._check()
        if self.graph_error is not None:
            raise self.graph_error
        if self.graph is not None:
            return self.graph
        by_title = {
            path.rsplit("/", 1)[-1].removesuffix(".md"): path for path in sorted(self.notes)
        }
        nodes, edges = [], []
        for path, (note_class, content) in sorted(self.notes.items()):
            if note_class not in ("knowledge", "lens"):
                continue
            nodes.append(
                GraphNode(
                    path=path,
                    title=path.rsplit("/", 1)[-1].removesuffix(".md"),
                    note_class=note_class,
                    lens_kind=self.lens_kinds.get(path, "concept") if note_class == "lens" else None,
                    aliases=(),
                    tags=(),
                    summary=self.summaries.get(path),
                    chars=len(content),
                )
            )
            for match in WIKILINK.finditer(content):
                target = match.group(1).strip()
                dst = by_title.get(target.rsplit("/", 1)[-1])
                if dst is None:
                    edges.append(GraphEdge(src=path, unresolved=target))
                elif self.notes[dst][0] in ("knowledge", "lens"):
                    edges.append(GraphEdge(src=path, dst=dst))
                else:
                    edges.append(GraphEdge(src=path, outside=True))
        return Graph(nodes=nodes, edges=edges, truncated=False)

    async def get_file(self, path: str) -> FileContent:
        self.calls.append(("get", path))
        self._check()
        if path in self.missing_notes:
            raise VaultError(errors.NOT_FOUND)
        if path in self.files:
            return FileContent(path, sha(self.files[path]), self.files[path])
        if path in self.notes:
            note_class, content = self.notes[path]
            return FileContent(path, sha(content), content, note_class)
        raise VaultError(errors.NOT_FOUND)

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
