"""`POST /v1/knowledge/rename`: moves and backlinks (write-plan section 4, "Rename and backlinks")."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.conftest import AUTH, write

SETTINGS = (
    "---\nanchor: settings\n"
    "knowledge_folders: [Library]\npersonal_folders: [Life]\nnever_folders: [Life/Diary]\n---\n"
)


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def note(mark: str | None, body: str = "Body.\n") -> str:
    if mark is None:
        return body
    return f"---\nanchor: {mark}\n---\n{body}"


@pytest.fixture(autouse=True)
def _base_settings(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    (vault / "Library").mkdir(exist_ok=True)
    (vault / "Life").mkdir(exist_ok=True)
    (vault / "Life" / "Diary").mkdir(exist_ok=True)
    (vault / "Elsewhere").mkdir(exist_ok=True)


async def _rename(client, path: str, new_path: str, if_sha256: str, changeset: str = "cs"):
    return await client.post(
        "/v1/knowledge/rename",
        json={"path": path, "new_path": new_path, "if_sha256": if_sha256, "changeset": changeset},
        headers=AUTH,
    )


async def test_moves_and_rewrites_every_backlink_form(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))
    linker = "See [[Old]], [[Old|label]], [[Old#heading]] and ![[Old]], also [[Library/Old]].\n"
    write(vault, "Library/Linker.md", note("knowledge", linker))
    old_sha = sha(note("knowledge"))

    resp = await _rename(client, "Library/Old.md", "Library/New.md", old_sha)
    assert resp.status == 200
    body = await resp.json()
    assert body["relinked"] == 1

    assert not (vault / "Library" / "Old.md").exists()
    new_body = (vault / "Library" / "New.md").read_text()
    assert new_body.endswith(note("knowledge"))
    linker_body = (vault / "Library" / "Linker.md").read_text()
    assert "[[New]]" in linker_body
    assert "[[New|label]]" in linker_body
    assert "[[New#heading]]" in linker_body
    assert "![[New]]" in linker_body
    assert "[[Old" not in linker_body
    assert "anchor_edited_by: claude" in linker_body


async def test_refused_when_a_personal_note_links_and_it_stays_untouched(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))
    write(vault, "Life/Дневник.md", note("personal", "See [[Old]].\n"))
    before = (vault / "Life" / "Дневник.md").read_bytes()

    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert await resp.read() == b""
    assert (vault / "Library" / "Old.md").exists()
    assert not (vault / "Library" / "New.md").exists()
    assert (vault / "Life" / "Дневник.md").read_bytes() == before


async def test_refused_when_a_never_note_links(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))
    write(vault, "Life/Diary/day.md", note("knowledge", "See [[Old]].\n"))  # folder rule -> never
    before = (vault / "Life" / "Diary" / "day.md").read_bytes()

    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Library" / "Old.md").exists()
    assert (vault / "Life" / "Diary" / "day.md").read_bytes() == before


async def test_refused_when_an_unclassified_note_links(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))
    write(vault, "Elsewhere/x.md", note(None, "See [[Old]].\n"))
    before = (vault / "Elsewhere" / "x.md").read_bytes()

    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Elsewhere" / "x.md").read_bytes() == before


async def test_refused_on_an_ambiguous_basename(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))
    write(vault, "Elsewhere/Old.md", note(None))  # a second file with the same basename, anywhere

    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Library" / "Old.md").exists()


async def test_refused_when_the_destination_is_taken(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))
    write(vault, "Library/New.md", note("knowledge"))
    before = (vault / "Library" / "New.md").read_bytes()

    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Library" / "New.md").read_bytes() == before
    assert (vault / "Library" / "Old.md").exists()


async def test_refused_when_the_destination_folder_is_not_knowledge(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))

    resp = await _rename(client, "Library/Old.md", "Life/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Library" / "Old.md").exists()
    assert not (vault / "Life" / "New.md").exists()


async def test_refused_when_the_destination_folder_does_not_exist(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))

    resp = await _rename(client, "Library/Old.md", "Library/Sub/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Library" / "Old.md").exists()


async def test_refused_when_touching_more_files_than_the_cap(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))
    for i in range(5):
        write(vault, f"Library/Linker{i}.md", note("knowledge", "See [[Old]].\n"))
    before = [(vault / "Library" / f"Linker{i}.md").read_bytes() for i in range(5)]

    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Library" / "Old.md").exists()
    after = [(vault / "Library" / f"Linker{i}.md").read_bytes() for i in range(5)]
    assert before == after


async def test_source_class_must_be_knowledge(client, vault: Path):
    write(vault, "Life/x.md", note("personal"))
    resp = await _rename(client, "Life/x.md", "Library/New.md", sha(note("personal")))
    assert resp.status == 403


async def test_cas_mismatch_on_the_source_is_412(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))
    resp = await _rename(client, "Library/Old.md", "Library/New.md", "0" * 64)
    assert resp.status == 412
    assert (vault / "Library" / "Old.md").exists()


async def test_missing_source_is_404(client, vault: Path):
    resp = await _rename(client, "Library/Missing.md", "Library/New.md", "0" * 64)
    assert resp.status == 404


async def test_rename_undo_restores_the_file_and_all_backlinks_byte_for_byte(client, vault: Path):
    original = note("knowledge")
    linker_before = note("knowledge", "See [[Old]] and [[Old|l]].\n")
    write(vault, "Library/Old.md", original)
    write(vault, "Library/Linker.md", linker_before)

    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(original), changeset="rn")
    assert resp.status == 200

    undo = await client.post("/v1/undo", params={"changeset": "rn"}, headers=AUTH)
    assert undo.status == 200
    assert (await undo.json()) == {"restored": 3, "refused": 0}

    assert not (vault / "Library" / "New.md").exists()
    assert (vault / "Library" / "Old.md").read_bytes() == original.encode()
    assert (vault / "Library" / "Linker.md").read_bytes() == linker_before.encode()
