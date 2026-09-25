"""Compare-and-swap writes (plan sections 5.4, 15)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from vaultd import store as store_module
from vaultd.store import Conflict, Store
from tests.conftest import AUTH, write

PATH = "Anchor/Memory/0001-abcdef.md"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def test_create_only_creates_folders_and_returns_the_hash(client, vault: Path, data_dir: Path) -> None:
    resp = await client.put(
        "/v1/file", params={"path": PATH}, json={"content": "факт", "if_sha256": None}, headers=AUTH
    )
    assert resp.status == 200
    assert (await resp.json()) == {"sha256": sha("факт")}
    assert (vault / PATH).read_text() == "факт"
    assert list((data_dir / "tmp").iterdir()) == []


async def test_create_only_on_an_existing_file_is_412_and_leaves_it_untouched(
    client, vault: Path, data_dir: Path
) -> None:
    write(vault, PATH, "yours")
    resp = await client.put(
        "/v1/file", params={"path": PATH}, json={"content": "mine", "if_sha256": None}, headers=AUTH
    )
    assert resp.status == 412
    assert (vault / PATH).read_text() == "yours"
    assert list((data_dir / "tmp").iterdir()) == []


def test_create_only_uses_link_so_a_racing_create_loses_to_nobody(store: Store, vault: Path, monkeypatch) -> None:
    """ob creates the file after our check would have passed: link still refuses."""
    real_link = os.link

    def racing_link(src, dst, **kwargs):
        write(vault, PATH, "ob got there first")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(store_module.os, "link", racing_link)
    with pytest.raises(Conflict):
        store.put(PATH, b"mine", None)
    assert (vault / PATH).read_text() == "ob got there first"


async def test_update_with_the_right_hash(client, vault: Path) -> None:
    write(vault, PATH, "old")
    resp = await client.put(
        "/v1/file", params={"path": PATH}, json={"content": "new", "if_sha256": sha("old")}, headers=AUTH
    )
    assert resp.status == 200
    assert (vault / PATH).read_text() == "new"


async def test_update_with_a_stale_hash_is_412(client, vault: Path) -> None:
    write(vault, PATH, "edited on the phone")
    resp = await client.put(
        "/v1/file", params={"path": PATH}, json={"content": "new", "if_sha256": sha("old")}, headers=AUTH
    )
    assert resp.status == 412
    assert (vault / PATH).read_text() == "edited on the phone"


async def test_update_of_a_missing_file_is_412(client, vault: Path) -> None:
    resp = await client.put(
        "/v1/file", params={"path": PATH}, json={"content": "new", "if_sha256": sha("old")}, headers=AUTH
    )
    assert resp.status == 412
    assert not (vault / PATH).exists()


def test_update_rechecks_after_writing_the_temp_file(store: Store, vault: Path, monkeypatch) -> None:
    """An edit that lands while the temp file is being written wins."""
    write(vault, PATH, "old")
    real_write_temp = Store._write_temp

    def slow_write_temp(self, tmp_fd, data):
        name = real_write_temp(self, tmp_fd, data)
        write(vault, PATH, "edited meanwhile")
        return name

    monkeypatch.setattr(Store, "_write_temp", slow_write_temp)
    with pytest.raises(Conflict):
        store.put(PATH, b"new", sha("old"))
    assert (vault / PATH).read_text() == "edited meanwhile"


async def test_delete_with_a_stale_hash_is_412(client, vault: Path) -> None:
    write(vault, PATH, "edited")
    resp = await client.delete("/v1/file", params={"path": PATH, "if_sha256": sha("old")}, headers=AUTH)
    assert resp.status == 412
    assert (vault / PATH).exists()


async def test_delete_with_the_right_hash(client, vault: Path) -> None:
    write(vault, PATH, "old")
    resp = await client.delete("/v1/file", params={"path": PATH, "if_sha256": sha("old")}, headers=AUTH)
    assert resp.status == 200
    assert not (vault / PATH).exists()


async def test_delete_of_a_missing_file_is_404(client) -> None:
    resp = await client.delete("/v1/file", params={"path": PATH, "if_sha256": sha("old")}, headers=AUTH)
    assert resp.status == 404


@pytest.mark.parametrize("fail_at", ["fsync", "replace"])
async def test_a_failure_mid_write_leaves_the_old_content(
    client, vault: Path, data_dir: Path, monkeypatch, fail_at: str
) -> None:
    write(vault, PATH, "old")

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(store_module.os, fail_at, boom)
    resp = await client.put(
        "/v1/file", params={"path": PATH}, json={"content": "new", "if_sha256": sha("old")}, headers=AUTH
    )
    assert resp.status == 500
    monkeypatch.undo()
    assert (vault / PATH).read_text() == "old"
    assert list((data_dir / "tmp").iterdir()) == []


@pytest.mark.parametrize(
    "body",
    [
        {"content": "x"},
        {"if_sha256": None},
        {"content": 1, "if_sha256": None},
        {"content": "x", "if_sha256": "short"},
        {"content": "x", "if_sha256": None, "extra": 1},
        ["content"],
    ],
)
async def test_malformed_bodies_are_400(client, vault: Path, body) -> None:
    resp = await client.put("/v1/file", params={"path": PATH}, json=body, headers=AUTH)
    assert resp.status == 400
    assert not (vault / PATH).exists()


async def test_a_body_over_64_kb_is_refused(client, vault: Path) -> None:
    resp = await client.put(
        "/v1/file",
        params={"path": PATH},
        json={"content": "я" * 40_000, "if_sha256": None},
        headers=AUTH,
    )
    assert resp.status == 413
    assert not (vault / PATH).exists()


async def test_purge_deletes_only_anchors_own_files(client, vault: Path, tmp_path: Path) -> None:
    write(vault, "Anchor/Memory/0001-abcdef.md", "fact")
    write(vault, "Anchor/Memory/0002-abcdef.md", "fact")
    write(vault, "Anchor/Journal/2026-09-25-abcdef.md", "day")
    write(vault, "Anchor/Memory/sub/keep.md", "user's")
    write(vault, "Anchor/Memory/.keep.md", "hidden")
    write(vault, "Anchor/Memory/keep.txt", "not md")
    write(vault, "Anchor/README.md", "user's")
    write(vault, "Бег.md", "user's")
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    os.symlink(outside, vault / "Anchor" / "Memory" / "link.md")

    first = await client.post("/v1/purge", headers=AUTH)
    assert (await first.json()) == {"deleted": 3}
    second = await client.post("/v1/purge", headers=AUTH)
    assert (await second.json()) == {"deleted": 0}
    remaining = sorted(str(p.relative_to(vault)) for p in vault.rglob("*") if p.is_file() or p.is_symlink())
    assert remaining == [
        "Anchor/Memory/.keep.md",
        "Anchor/Memory/keep.txt",
        "Anchor/Memory/link.md",
        "Anchor/Memory/sub/keep.md",
        "Anchor/README.md",
        "Бег.md",
    ]
    assert outside.read_text() == "outside"


async def test_purge_of_an_empty_vault(client) -> None:
    resp = await client.post("/v1/purge", headers=AUTH)
    assert (await resp.json()) == {"deleted": 0}


async def test_anchor_scope_file_that_is_not_utf8_is_422(client, vault: Path) -> None:
    write(vault, PATH, b"\xff\xfe")
    got = await client.get("/v1/file", params={"path": PATH}, headers=AUTH)
    assert got.status == 422
