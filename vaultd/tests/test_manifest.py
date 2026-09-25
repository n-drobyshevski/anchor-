"""The manifest and its cache (plan sections 5.4, 15)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from vaultd.manifest import Manifest
from tests.conftest import AUTH, write


async def test_scopes(client, vault: Path) -> None:
    write(vault, "Anchor/Memory/0001-abcdef.md", "---\nanchor: fact\n---\n")
    write(vault, "Anchor/Journal/2026-09-25-abcdef.md", "day")
    write(vault, "Anchor/Memory/Утро.md", "user-made fact file, no frontmatter")
    write(vault, "Anchor/README.md", "not in a writable folder, not opted in")
    write(vault, "Anchor/Memory/sub/x.md", "---\nanchor: read\n---\n")
    write(vault, "Anchor/Memory/x.base", "not md")
    write(vault, "Notes/Бег.md", "---\nanchor: read\n---\n")
    manifest = await (await client.get("/v1/manifest", headers=AUTH)).json()
    assert [(f["path"], f["scope"]) for f in manifest["files"]] == [
        ("Anchor/Journal/2026-09-25-abcdef.md", "anchor"),
        ("Anchor/Memory/0001-abcdef.md", "anchor"),
        ("Anchor/Memory/sub/x.md", "note"),
        ("Anchor/Memory/Утро.md", "anchor"),
        ("Notes/Бег.md", "note"),
    ]
    day = manifest["files"][0]
    assert day["sha256"] == hashlib.sha256(b"day").hexdigest()
    assert day["size"] == 3


def test_cache_skips_unchanged_files(vault: Path) -> None:
    write(vault, "Anchor/Memory/a.md", "a")
    manifest = Manifest(vault)
    manifest.scan()
    assert manifest.last_reads == 1
    manifest.scan()
    assert manifest.last_reads == 0


def test_a_rewrite_with_the_same_size_and_mtime_is_rehashed(vault: Path) -> None:
    """ob sets mtimes from the server, so mtime alone is not a safe key."""
    path = write(vault, "Anchor/Memory/a.md", "aaaa")
    manifest = Manifest(vault)
    [before] = manifest.scan()
    st = path.stat()

    # A new inode: write elsewhere, rename over, restore size and mtime.
    replacement = write(vault, "Anchor/Memory/.tmp", "bbbb")
    os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(replacement, path)
    [after] = manifest.scan()
    assert manifest.last_reads == 1
    assert after.sha256 == hashlib.sha256(b"bbbb").hexdigest() != before.sha256

    # Same inode, rewritten in place, mtime restored: only ctime moved.
    st = path.stat()
    path.write_text("cccc")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    [again] = manifest.scan()
    assert manifest.last_reads == 1
    assert again.sha256 == hashlib.sha256(b"cccc").hexdigest()


def test_losing_the_opt_in_drops_the_note(vault: Path) -> None:
    path = write(vault, "note.md", "---\nanchor: read\n---\n")
    manifest = Manifest(vault)
    assert [e.path for e in manifest.scan()] == ["note.md"]
    path.write_text("---\nanchor: nope\n---\n")
    assert manifest.scan() == []
    path.unlink()
    assert manifest.scan() == []
