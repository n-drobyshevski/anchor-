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


def test_plan_rename_itself_refuses_a_taken_destination(vault: Path):
    """`plan_rename` is a pure validator (it touches no disk beyond
    reading) and must refuse a taken destination on its own -- not by
    relying on `perform_rename`'s create-only write to fail later,
    which is what an HTTP-level test alone would actually be proving."""
    from vaultd import knowledge

    write(vault, "Library/Old.md", note("knowledge"))
    write(vault, "Library/New.md", note("knowledge"))
    with pytest.raises(knowledge.Refused):
        knowledge.plan_rename(vault, "Library/Old.md", "Library/New.md", sha(note("knowledge")))


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
    # Elsewhere carries no folder rule at all, unlike Life (personal):
    # this isolates the "destination folder's OWN rule" check from the
    # separate "destination content's class" check, which a personal
    # folder would also trip on its own.
    resp = await _rename(client, "Library/Old.md", "Elsewhere/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Library" / "Old.md").exists()
    assert not (vault / "Elsewhere" / "New.md").exists()


async def test_refused_when_the_destination_folder_is_personal(client, vault: Path):
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


async def test_refused_when_an_anchor_fact_file_links(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))
    write(vault, "Anchor/Memory/0001-abcdef.md", "See [[Old]].\n")
    before = (vault / "Anchor" / "Memory" / "0001-abcdef.md").read_bytes()

    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert await resp.read() == b""
    assert (vault / "Library" / "Old.md").exists()
    assert not (vault / "Library" / "New.md").exists()
    assert (vault / "Anchor" / "Memory" / "0001-abcdef.md").read_bytes() == before


async def test_ambiguous_basename_counts_an_anchor_file(client, vault: Path):
    write(vault, "Library/Old.md", note("knowledge"))
    write(vault, "Anchor/Memory/Old.md", "a fact file that happens to share the basename\n")
    before = (vault / "Anchor" / "Memory" / "Old.md").read_bytes()

    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Library" / "Old.md").exists()
    assert (vault / "Anchor" / "Memory" / "Old.md").read_bytes() == before


async def test_rename_rolls_back_on_a_mid_rename_backlink_race(client, vault: Path, monkeypatch):
    """`ob` lands a sync on a backlink file between planning and writing it."""
    from vaultd.store import Store

    write(vault, "Library/Old.md", note("knowledge"))
    write(vault, "Library/Linker.md", note("knowledge", "See [[Old]].\n"))
    old_before = (vault / "Library" / "Old.md").read_bytes()

    real_put_unchecked = Store.put_unchecked

    def racing_put_unchecked(self, rel, data, if_sha256):
        if rel == "Library/Linker.md":
            write(vault, "Library/Linker.md", "edited on the phone\n")
        return real_put_unchecked(self, rel, data, if_sha256)

    monkeypatch.setattr(Store, "put_unchecked", racing_put_unchecked)
    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note("knowledge")))
    monkeypatch.undo()

    assert resp.status == 412
    assert not (vault / "Library" / "New.md").exists()
    assert (vault / "Library" / "Old.md").read_bytes() == old_before
    # The concurrent edit itself is exactly what CAS is supposed to
    # preserve -- rollback restores vaultd's own pre-images, never the
    # phone's edit that raced it.
    assert (vault / "Library" / "Linker.md").read_bytes() == b"edited on the phone\n"


async def test_mid_rename_failure_still_records_the_changeset(client, vault: Path, monkeypatch):
    """The delete of the old path fails after the new one was created.

    Both paths are left in place -- a duplicate -- but the create is
    still recorded, so undoing that changeset removes the duplicate.
    """
    from vaultd.store import Missing, Store

    write(vault, "Library/Old.md", note("knowledge"))

    def failing_delete(self, rel, if_sha256):
        raise Missing

    monkeypatch.setattr(Store, "delete_unchecked", failing_delete)
    resp = await _rename(client, "Library/Old.md", "Library/New.md", sha(note("knowledge")), changeset="mrf")
    monkeypatch.undo()

    assert resp.status == 500
    assert (vault / "Library" / "Old.md").exists()
    assert (vault / "Library" / "New.md").exists()

    changes = await (await client.get("/v1/changes", headers=AUTH)).json()
    entry = next(c for c in changes["changes"] if c["id"] == "mrf")
    assert entry["files"] == [{"path": "Library/New.md", "sha256": sha(note("knowledge"))}]

    undo = await client.post("/v1/undo", params={"changeset": "mrf"}, headers=AUTH)
    assert (await undo.json()) == {"restored": 1, "refused": 0}
    assert not (vault / "Library" / "New.md").exists()
    assert (vault / "Library" / "Old.md").exists()


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
