"""A rename's own budget, separate from the content-write cap
(write-plan rev. 3, anchor-claude-write-plan.md section 14)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from vaultd.config import FILES_PER_CHANGESET, MOVE_FILES_PER_CHANGESET, MOVES_PER_DAY
from tests.conftest import AUTH, write

SETTINGS = "---\nanchor: settings\nknowledge_folders: [Library]\n---\n"


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def note(mark: str = "knowledge", body: str = "Body.\n") -> str:
    return f"---\nanchor: {mark}\n---\n{body}"


@pytest.fixture(autouse=True)
def _base_settings(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    (vault / "Library").mkdir(exist_ok=True)


async def _put(client, path: str, content: str, if_sha256: str | None, changeset: str = "cs"):
    return await client.put(
        "/v1/knowledge",
        params={"path": path},
        json={"content": content, "if_sha256": if_sha256, "changeset": changeset},
        headers=AUTH,
    )


async def _rename(client, path: str, new_path: str, if_sha256: str, changeset: str = "cs"):
    return await client.post(
        "/v1/knowledge/rename",
        json={"path": path, "new_path": new_path, "if_sha256": if_sha256, "changeset": changeset},
        headers=AUTH,
    )


async def test_six_content_writes_refused_at_the_sixth(client, vault: Path):
    """Unaffected by rev. 3: FILES_PER_CHANGESET (5) still governs
    plain content writes on their own."""
    assert FILES_PER_CHANGESET == 5
    for i in range(FILES_PER_CHANGESET):
        resp = await _put(client, f"Library/F{i}.md", "x", None, changeset="cs")
        assert resp.status == 200
    resp = await _put(client, "Library/F5.md", "x", None, changeset="cs")
    assert resp.status == 403
    assert not (vault / "Library" / "F5.md").exists()


async def test_content_cap_full_still_allows_a_rename_in_the_same_changeset(client, vault: Path):
    """The content-write cap and the move cap are independent counters
    on the same vaultd changeset (rev. 3): a changeset that has already
    spent its 5 content files can still rename a note."""
    write(vault, "Library/Old.md", note())
    for i in range(FILES_PER_CHANGESET):
        resp = await _put(client, f"Library/F{i}.md", "x", None, changeset="cs")
        assert resp.status == 200

    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note()), changeset="cs")
    assert resp.status == 200


async def test_move_cap_full_still_allows_a_content_write_in_the_same_changeset(client, vault: Path):
    write(vault, "Library/Old.md", note())
    for i in range(MOVE_FILES_PER_CHANGESET - 1):
        write(vault, f"Library/Linker{i}.md", note("knowledge", "See [[Old]].\n"))
    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note()), changeset="cs")
    assert resp.status == 200
    body = await resp.json()
    assert body["files_moved"] == MOVE_FILES_PER_CHANGESET

    resp2 = await _put(client, "Library/Content.md", "x", None, changeset="cs")
    assert resp2.status == 200


async def test_sixty_first_move_in_a_day_is_refused(client, vault: Path):
    """MOVES_PER_DAY (60): three renames of 20 files each (the
    per-changeset cap) exhaust it exactly; a fourth changeset -- still
    well inside CHANGESETS_PER_HOUR (4), so that cap never interferes --
    moving even one more file is refused."""
    assert MOVES_PER_DAY == 60
    assert MOVE_FILES_PER_CHANGESET == 20
    for n in range(3):
        write(vault, f"Library/Old{n}.md", note())
        for i in range(MOVE_FILES_PER_CHANGESET - 1):
            write(vault, f"Library/L{n}-{i}.md", note("knowledge", f"See [[Old{n}]].\n"))
        resp = await _rename(
            client, f"Library/Old{n}.md", f"Library/New{n}.md", sha(note()), changeset=f"m{n}"
        )
        assert resp.status == 200, n
        body = await resp.json()
        assert body["files_moved"] == MOVE_FILES_PER_CHANGESET

    write(vault, "Library/Old3.md", note())
    resp = await _rename(client, "Library/Old3.md", "Library/New3.md", sha(note()), changeset="m3")
    assert resp.status == 403
    assert (vault / "Library" / "Old3.md").exists()
    assert not (vault / "Library" / "New3.md").exists()
