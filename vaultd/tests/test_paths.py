"""The path rules, as a table of hostile inputs (plan sections 5.4, 15).

These run against the HTTP API, not just `paths.py`, because the API is
the boundary: a helper that refuses correctly is worth nothing if a
route forgets to call it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vaultd import paths
from tests.conftest import AUTH, write

NEW = {"content": "x", "if_sha256": None}

REFUSED_FOR_WRITING = [
    "../x.md",
    "../../etc/passwd",
    "Anchor/../x.md",
    "Anchor/Memory/../../x.md",
    "/etc/passwd",
    "/Anchor/Memory/x.md",
    "Anchor\\Memory\\x.md",
    "Anchor/Memory/x\\y.md",
    "Anchor/Memory/x\x00.md",
    "Anchor/Memory/x.md/..",
    "Anchor/Memory/./x.md",
    "Anchor/Memory//x.md",
    "Anchor/Memory/.hidden.md",
    "Anchor/Memory/sub/x.md",
    "Anchor/Journal.md",
    "Anchor/x.md",
    "Anchor/Memory/x.txt",
    "Anchor/Memory/x.MD",
    "Anchor/Memory/x.canvas",
    "Anchor/Memory/",
    "Anchor/Memory",
    "anchor/memory/x.md",
    "Notes/x.md",
    ".obsidian/app.json",
    "",
]


@pytest.mark.parametrize("path", REFUSED_FOR_WRITING)
async def test_writes_outside_anchor_folders_are_refused(client, vault: Path, path: str) -> None:
    put = await client.put("/v1/file", params={"path": path}, json=NEW, headers=AUTH)
    assert put.status in (400, 403)
    delete = await client.delete(
        "/v1/file", params={"path": path, "if_sha256": "0" * 64}, headers=AUTH
    )
    assert delete.status in (400, 403)
    # Nothing at all was created anywhere in the vault.
    assert [p for p in vault.rglob("*")] == []


@pytest.mark.parametrize("path", ["Anchor/Memory/0001-abcdef.md", "Anchor/Journal/2026-09-25-abcdef.md", "Anchor/Memory/Утро.md"])
def test_the_writable_set_is_exactly_the_two_folders(path: str) -> None:
    assert paths.is_writable(paths.parse_rel(path))


@pytest.mark.parametrize("raw", ["", "/x.md", "a\\b.md", "a\x00.md", "a/../b.md", "a/./b.md", "a//b.md", "a/", "\udcff.md"])
def test_malformed_paths(raw: str) -> None:
    with pytest.raises(paths.Malformed):
        paths.parse_rel(raw)


async def test_a_symlinked_folder_out_of_the_vault_is_refused_for_writing(
    client, vault: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "Anchor").mkdir()
    os.symlink(outside, vault / "Anchor" / "Memory")
    resp = await client.put(
        "/v1/file", params={"path": "Anchor/Memory/x.md"}, json=NEW, headers=AUTH
    )
    assert resp.status == 403
    assert list(outside.iterdir()) == []


async def test_a_symlinked_file_is_refused_for_reading_writing_and_deleting(
    client, vault: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("---\nanchor: read\n---\nsecret\n")
    (vault / "Anchor" / "Memory").mkdir(parents=True)
    os.symlink(secret, vault / "Anchor" / "Memory" / "link.md")
    os.symlink(secret, vault / "link-note.md")

    for path in ("Anchor/Memory/link.md", "link-note.md"):
        got = await client.get("/v1/file", params={"path": path}, headers=AUTH)
        assert got.status == 404
    put = await client.put(
        "/v1/file",
        params={"path": "Anchor/Memory/link.md"},
        json={"content": "overwritten", "if_sha256": "0" * 64},
        headers=AUTH,
    )
    assert put.status in (403, 412)
    delete = await client.delete(
        "/v1/file", params={"path": "Anchor/Memory/link.md", "if_sha256": "0" * 64}, headers=AUTH
    )
    assert delete.status in (403, 404)
    assert secret.read_text().endswith("secret\n")

    manifest = await (await client.get("/v1/manifest", headers=AUTH)).json()
    assert manifest["files"] == []


async def test_a_symlinked_folder_is_refused_for_reading(client, vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside, "note.md", "---\nanchor: read\n---\nbody\n")
    os.symlink(outside, vault / "Linked")
    got = await client.get("/v1/file", params={"path": "Linked/note.md"}, headers=AUTH)
    assert got.status == 404
    manifest = await (await client.get("/v1/manifest", headers=AUTH)).json()
    assert manifest["files"] == []


async def test_dot_folders_are_never_listed_or_readable(client, vault: Path) -> None:
    opted_in = "---\nanchor: read\n---\nbody\n"
    write(vault, ".obsidian/workspace.md", opted_in)
    write(vault, ".trash/old.md", opted_in)
    write(vault, ".hidden.md", opted_in)
    write(vault, "Anchor/Memory/.x.md", "x")
    manifest = await (await client.get("/v1/manifest", headers=AUTH)).json()
    assert manifest["files"] == []
    for path in (".obsidian/workspace.md", ".trash/old.md", ".hidden.md", "Anchor/Memory/.x.md"):
        got = await client.get("/v1/file", params={"path": path}, headers=AUTH)
        assert got.status == 404
