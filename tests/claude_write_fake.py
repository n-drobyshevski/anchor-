"""A minimal in-memory stand-in for vaultd's knowledge routes (W2b).

Not vaultd: the bot's tests import nothing from vaultd
(tests/test_vault_isolation.py). This fake implements exactly the
`VaultClient` methods app/web/claude_write.py calls, with just enough
behaviour -- create-only fails if the name is taken, an update needs a
matching hash, a path in `refuse` always 403s, `list_changes`/
`undo_changeset` read back what this fake itself recorded -- to drive
the bot-level tests without reimplementing vaultd's own class boundary
or backlink rewriting, which vaultd/'s own suite already covers.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field

from aiohttp import web
from aiohttp.test_utils import TestServer

from app.vault import errors
from app.vault.client import (
    ChangeEntry,
    ChangeFile,
    KnowledgeContent,
    PutResult,
    RenameResult,
    Tree,
    UndoResult,
)
from app.vault.errors import VaultError


def sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class _Entry:
    path: str
    pre_image: str | None
    written_sha256: str | None


@dataclass
class FakeKnowledgeVault:
    files: dict[str, str] = field(default_factory=dict)
    # path -> refused ("class boundary", a cap vaultd itself would
    # enforce, etc.) -- every call touching it gets vaultd's bare 403.
    refuse: set[str] = field(default_factory=set)
    changesets: dict[str, list[_Entry]] = field(default_factory=dict)
    undone: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)
    # Set to force the next rename's `relinked` count, for cap tests.
    next_relinked: int = 0
    # Rev. 3: set to force the next put/rename's `folders_created`
    # (vaultd's own count of new folders it built), for cap/digest
    # tests. Overrides `files_moved` too, when a test needs a shape
    # `1 + next_relinked` would not produce (e.g. MidRenameFailure).
    next_folders_created: int = 0
    next_files_moved: int | None = None
    tree: Tree = field(default_factory=lambda: Tree(folders=[], notes=[], truncated=False))
    down: bool = False

    def _check_down(self) -> None:
        if self.down:
            raise VaultError(errors.UNAVAILABLE)

    async def get_knowledge(self, path: str) -> KnowledgeContent:
        self.calls.append(("GET", path))
        self._check_down()
        if path in self.refuse:
            raise VaultError(errors.REFUSED)
        if path not in self.files:
            raise VaultError(errors.NOT_FOUND)
        content = self.files[path]
        return KnowledgeContent(path=path, sha256=sha(content), content=content)

    async def put_knowledge(self, path: str, content: str, if_sha256, changeset: str) -> PutResult:
        self.calls.append(("PUT", path))
        self._check_down()
        if path in self.refuse:
            raise VaultError(errors.REFUSED)
        current = self.files.get(path)
        if if_sha256 is None:
            if current is not None:
                raise VaultError(errors.REFUSED)
            pre_image = None
        else:
            if current is None:
                raise VaultError(errors.NOT_FOUND)
            if sha(current) != if_sha256:
                raise VaultError(errors.CONFLICT)
            pre_image = current
        new_sha = sha(content)
        self.files[path] = content
        self.changesets.setdefault(changeset, []).append(_Entry(path, pre_image, new_sha))
        return PutResult(sha256=new_sha, folders_created=self.next_folders_created)

    async def rename_knowledge(self, path: str, new_path: str, if_sha256: str, changeset: str) -> RenameResult:
        self.calls.append(("RENAME", path))
        self._check_down()
        if path in self.refuse or new_path in self.refuse:
            raise VaultError(errors.REFUSED)
        current = self.files.get(path)
        if current is None:
            raise VaultError(errors.NOT_FOUND)
        if sha(current) != if_sha256:
            raise VaultError(errors.CONFLICT)
        if new_path in self.files:
            raise VaultError(errors.REFUSED)
        new_sha = sha(current)
        del self.files[path]
        self.files[new_path] = current
        entries = [_Entry(new_path, None, new_sha), _Entry(path, current, None)]
        self.changesets.setdefault(changeset, []).extend(entries)
        files_moved = self.next_files_moved if self.next_files_moved is not None else 1 + self.next_relinked
        return RenameResult(
            path=new_path,
            sha256=new_sha,
            relinked=self.next_relinked,
            folders_created=self.next_folders_created,
            files_moved=files_moved,
        )

    async def knowledge_tree(self) -> Tree:
        self.calls.append(("GET", "/v1/knowledge/tree"))
        self._check_down()
        return self.tree

    async def list_changes(self) -> list[ChangeEntry]:
        self.calls.append(("GET", "/v1/changes"))
        self._check_down()
        out = []
        for i, (ref, entries) in enumerate(self.changesets.items()):
            out.append(
                ChangeEntry(
                    id=ref,
                    kind="write",
                    time=datetime.datetime(2026, 9, 27, 12, i, tzinfo=datetime.timezone.utc),
                    undone=ref in self.undone,
                    files=[ChangeFile(path=e.path, sha256=e.written_sha256) for e in entries],
                )
            )
        return out

    async def undo_changeset(self, vault_ref: str) -> UndoResult:
        self.calls.append(("POST", "/v1/undo"))
        self._check_down()
        entries = self.changesets.get(vault_ref, [])
        restored = 0
        refused = 0
        for entry in reversed(entries):
            current = self.files.get(entry.path)
            current_sha = sha(current) if current is not None else None
            if current_sha != entry.written_sha256:
                refused += 1
                continue
            if entry.pre_image is None:
                self.files.pop(entry.path, None)
            else:
                self.files[entry.path] = entry.pre_image
            restored += 1
        if restored:
            self.undone.add(vault_ref)
        return UndoResult(restored=restored, refused=refused)


# --- a tiny aiohttp server speaking vaultd's own wire shapes -------------
#
# Wraps a FakeKnowledgeVault so a test can point a real VaultClient
# (settings.VAULT_URL) at it -- used where a test wants the real HTTP
# round trip (Telegram commands, the digest, the wiring test) rather
# than swapping in the fake object directly through vault_client_factory.


async def _make_app(vault: FakeKnowledgeVault) -> web.Application:
    async def get_knowledge(request: web.Request) -> web.Response:
        path = request.query.get("path", "")
        try:
            content = await vault.get_knowledge(path)
        except VaultError as exc:
            return web.json_response({"error": exc.code}, status=_status(exc.code))
        return web.json_response({"path": content.path, "sha256": content.sha256, "content": content.content})

    async def put_knowledge(request: web.Request) -> web.Response:
        path = request.query.get("path", "")
        body = await request.json()
        try:
            result = await vault.put_knowledge(path, body["content"], body["if_sha256"], body["changeset"])
        except VaultError as exc:
            if exc.code == errors.REFUSED:
                return web.Response(status=403)
            return web.json_response({"error": exc.code}, status=_status(exc.code))
        return web.json_response({"sha256": result.sha256, "folders_created": result.folders_created})

    async def rename_knowledge(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            result = await vault.rename_knowledge(
                body["path"], body["new_path"], body["if_sha256"], body["changeset"]
            )
        except VaultError as exc:
            if exc.code == errors.REFUSED:
                return web.Response(status=403)
            return web.json_response({"error": exc.code}, status=_status(exc.code))
        return web.json_response(
            {
                "path": result.path,
                "sha256": result.sha256,
                "relinked": result.relinked,
                "folders_created": result.folders_created,
                "files_moved": result.files_moved,
            }
        )

    async def knowledge_tree(request: web.Request) -> web.Response:
        tree = await vault.knowledge_tree()
        return web.json_response(
            {
                "folders": tree.folders,
                "notes": [
                    {"path": n.path, "title": n.title, "class": n.note_class} for n in tree.notes
                ],
                "truncated": tree.truncated,
            }
        )

    async def list_changes(request: web.Request) -> web.Response:
        changes = await vault.list_changes()
        return web.json_response(
            {
                "changes": [
                    {
                        "id": c.id,
                        "kind": c.kind,
                        "time": c.time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "undone": c.undone,
                        "files": [{"path": f.path, "sha256": f.sha256} for f in c.files],
                    }
                    for c in changes
                ]
            }
        )

    async def undo(request: web.Request) -> web.Response:
        vault_ref = request.query.get("changeset", "")
        result = await vault.undo_changeset(vault_ref)
        return web.json_response({"restored": result.restored, "refused": result.refused})

    app = web.Application()
    app.router.add_get("/v1/knowledge", get_knowledge)
    app.router.add_put("/v1/knowledge", put_knowledge)
    app.router.add_post("/v1/knowledge/rename", rename_knowledge)
    app.router.add_get("/v1/knowledge/tree", knowledge_tree)
    app.router.add_get("/v1/changes", list_changes)
    app.router.add_post("/v1/undo", undo)
    return app


def _status(code: str) -> int:
    return {errors.NOT_FOUND: 404, errors.CONFLICT: 412, errors.REFUSED: 403}.get(code, 500)


async def start_fake_vaultd(vault: FakeKnowledgeVault | None = None) -> tuple[FakeKnowledgeVault, TestServer]:
    vault = vault if vault is not None else FakeKnowledgeVault()
    server = TestServer(await _make_app(vault), host="127.0.0.1")
    await server.start_server()
    vault.url = f"http://127.0.0.1:{server.port}"  # type: ignore[attr-defined]
    return vault, server
