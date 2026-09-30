"""An in-memory vault with vaultd's semantics, for the sync pass's tests.

Not vaultd: the bot's tests import nothing from it. This fake answers
the same questions the same way -- create-only fails if the file
exists, an update or delete with a stale hash is a conflict, only
Anchor's own folders are writable -- and lets a test reach in between
calls to play the user editing a file on their phone.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from typing import Callable

from app.vault import errors
from app.vault.client import (
    ChangeEntry,
    ChangeFile,
    EchoPut,
    FileContent,
    Graph,
    GraphEdge,
    GraphNode,
    Manifest,
    ManifestEntry,
    NotesSummary,
    ServiceStatus,
    UndoResult,
)
from app.vault.errors import VaultError

WRITABLE = re.compile(r"^Anchor/(Memory|Journal|Reports)/[^/]+\.md$")
# Lens L3: vaultd before `Reports/` was writable. A test sets
# `fake.writable = PRE_REPORTS_WRITABLE` to play the bot deployed ahead
# of vaultd, whose report writes come back REFUSED.
PRE_REPORTS_WRITABLE = re.compile(r"^Anchor/(Memory|Journal)/[^/]+\.md$")
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
        # Lens L3: which paths this vaultd takes writes to (see
        # PRE_REPORTS_WRITABLE).
        self.writable: re.Pattern[str] = WRITABLE
        # L3: frontmatter aliases the graph reports, path -> aliases.
        self.aliases: dict[str, tuple[str, ...]] = {}
        # L4: Echo's inbox writer (`PUT /v1/echo/inbox`). The inbox
        # folder, or None for "no inbox" (every put REFUSED); each Echo
        # changeset vaultd recorded, vault_ref -> {"path", "sha256",
        # "undone"}; an error to raise once instead of performing a put
        # (`echo_put_error`: REFUSED plays a 403, UNAVAILABLE a transport
        # failure before the write) or once after performing it
        # (`echo_crash_after_put`: the note is written, the answer lost).
        # Notes land in `notes` as knowledge, where vaultd would list them.
        self.echo_inbox: str | None = "Echo/Inbox"
        self.echo_changesets: dict[str, dict] = {}
        self.echo_put_error: Exception | None = None
        self.echo_crash_after_put: Exception | None = None
        # L4: the changeset ids a `POST /v1/undo` was asked for, with the
        # writer named; and an error to raise once after performing an
        # undo (its answer lost).
        self.undo_calls: list[tuple[str, str]] = []
        self.echo_undo_crash_after: Exception | None = None

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
            if self.writable.match(path)
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
                    aliases=self.aliases.get(path, ()),
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
        if not self.writable.match(path):
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
        if not self.writable.match(path):
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
        doomed = [p for p in self.files if self.writable.match(p)]
        for path in doomed:
            del self.files[path]
        self.purged += len(doomed)
        return len(doomed)

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] in ("put", "delete", "purge")]

    # -- L4: Echo's inbox writer and undo --------------------------------------------

    async def put_echo_note(self, name: str, content: str, changeset: str) -> EchoPut:
        """vaultd's `PUT /v1/echo/inbox`, in the ways the bot can see:
        a replayed changeset answers the note it already made, a missing
        inbox or a bad basename is REFUSED, a taken name gets ` 2`..` 9`."""
        self.calls.append(("echo_put", changeset))
        self._check()
        recorded = self.echo_changesets.get(changeset)
        if recorded is not None:
            return EchoPut(recorded["path"].rsplit("/", 1)[-1], recorded["sha256"], True)
        if self.echo_put_error is not None:
            exc, self.echo_put_error = self.echo_put_error, None
            raise exc
        if self.echo_inbox is None or "/" in name or not name.endswith(".md") or name.startswith("."):
            raise VaultError(errors.REFUSED)
        stem = name[: -len(".md")]
        for candidate in [name] + [f"{stem} {n}.md" for n in range(2, 10)]:
            path = f"{self.echo_inbox}/{candidate}"
            if path not in self.notes and path not in self.files:
                break
        else:
            raise VaultError(errors.REFUSED)
        self.notes[path] = ("knowledge", content)
        self.echo_changesets[changeset] = {"path": path, "sha256": sha(content), "undone": False}
        if self.echo_crash_after_put is not None:
            exc, self.echo_crash_after_put = self.echo_crash_after_put, None
            raise exc
        return EchoPut(candidate, sha(content), False)

    async def undo_changeset(self, vault_ref: str, *, writer: str = "claude") -> UndoResult:
        """vaultd's `POST /v1/undo` for Echo's changesets: NOT_FOUND for
        an unknown one, REFUSED for the wrong writer, and a note edited
        since (or already gone) refused by compare-and-swap."""
        self.undo_calls.append((vault_ref, writer))
        self._check()
        recorded = self.echo_changesets.get(vault_ref)
        if recorded is None:
            raise VaultError(errors.NOT_FOUND)
        if writer != "echo":
            raise VaultError(errors.REFUSED)
        current = self.notes.get(recorded["path"])
        if current is None or sha(current[1]) != recorded["sha256"]:
            return UndoResult(restored=0, refused=1)
        del self.notes[recorded["path"]]
        recorded["undone"] = True
        if self.echo_undo_crash_after is not None:
            exc, self.echo_undo_crash_after = self.echo_undo_crash_after, None
            raise exc
        return UndoResult(restored=1, refused=0)

    async def list_changes(self) -> list[ChangeEntry]:
        """vaultd's `GET /v1/changes`, for Echo's changesets: each write
        with its path, hash and undone flag."""
        self.calls.append(("list_changes", ""))
        self._check()
        return [
            ChangeEntry(
                id=ref,
                kind="write",
                time=datetime.datetime(2026, 9, 30, tzinfo=datetime.timezone.utc),
                undone=recorded["undone"],
                files=[ChangeFile(path=recorded["path"], sha256=recorded["sha256"])],
                writer="echo",
            )
            for ref, recorded in self.echo_changesets.items()
        ]
