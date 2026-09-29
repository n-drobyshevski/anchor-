"""Lens notes on the knowledge routes (lens plan sections 3-4).

Lens is a kind of knowledge for every reader -- `GET /v1/knowledge` and
the tree include it -- and for no writer: `PUT /v1/knowledge` and the
rename refuse a lens note as the note written, as the destination, and
as a backlink a rename would have to rewrite. Only the user changes the
lens. Every refusal is the same bare 403 as any other class refusal.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from tests.conftest import AUTH, write

SETTINGS = (
    "---\nanchor: settings\n"
    "knowledge_folders: [Library]\npersonal_folders: [Life]\nnever_folders: [Life/Diary]\n"
    "lens_folders: [Lens]\nlens_person_folders: [Lens/People]\n---\n"
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
    (vault / "Lens" / "People").mkdir(parents=True, exist_ok=True)


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


def _reasons(caplog) -> list[str]:
    return [r.__dict__["reason"] for r in caplog.records if r.__dict__.get("event") == "knowledge_refused"]


# -- reading ----------------------------------------------------------------------


async def test_a_lens_note_reads_like_knowledge_and_says_its_class(client, vault: Path):
    write(vault, "Lens/People/Fisher.md", note(None, "Capitalist realism.\n"))
    write(vault, "Library/CCRU.md", note(None))
    lens = await client.get("/v1/knowledge", params={"path": "Lens/People/Fisher.md"}, headers=AUTH)
    assert lens.status == 200
    body = await lens.json()
    assert body["content"] == "Capitalist realism.\n"
    assert body["class"] == "lens"
    assert body["sha256"] == sha("Capitalist realism.\n")
    plain = await client.get("/v1/knowledge", params={"path": "Library/CCRU.md"}, headers=AUTH)
    assert (await plain.json())["class"] == "knowledge"


async def test_a_lens_mark_inside_a_personal_folder_stays_unreadable(client, vault: Path):
    write(vault, "Life/Мысль.md", note("lens"))
    resp = await client.get("/v1/knowledge", params={"path": "Life/Мысль.md"}, headers=AUTH)
    assert resp.status == 404


async def test_the_tree_lists_lens_notes_with_their_class_but_not_lens_folders(client, vault: Path):
    write(vault, "Library/CCRU.md", note(None))
    write(vault, "Library/Marked.md", note("lens"))  # a knowledge folder outranks the mark
    write(vault, "Lens/Cybernetics.md", note(None))
    write(vault, "Lens/People/Fisher.md", note(None))
    write(vault, "Lens/Excluded.md", note("knowledge"))
    write(vault, "Lens/Private.md", note("personal"))
    write(vault, "Elsewhere/Ashby.md", note("lens"))

    resp = await client.get("/v1/knowledge/tree", headers=AUTH)
    tree = await resp.json()
    assert tree["notes"] == [
        {"path": "Elsewhere/Ashby.md", "title": "Ashby", "class": "lens"},
        {"path": "Lens/Cybernetics.md", "title": "Cybernetics", "class": "lens"},
        {"path": "Lens/Excluded.md", "title": "Excluded", "class": "knowledge"},
        {"path": "Lens/People/Fisher.md", "title": "Fisher", "class": "lens"},
        {"path": "Library/CCRU.md", "title": "CCRU", "class": "knowledge"},
        {"path": "Library/Marked.md", "title": "Marked", "class": "knowledge"},
    ]
    # `folders` is where a new note may go; nothing new goes into the lens.
    assert tree["folders"] == ["Library"]


# -- writing ----------------------------------------------------------------------


async def test_updating_a_lens_note_is_refused(client, vault: Path, caplog):
    for rel, content in (
        ("Lens/Cybernetics.md", note(None)),
        ("Lens/People/Fisher.md", note("lens")),
        ("Elsewhere/Ashby.md", note("lens")),
    ):
        write(vault, rel, content)
        with caplog.at_level(logging.INFO):
            resp = await _put(client, rel, content + "More.\n", sha(content))
        assert resp.status == 403
        assert await resp.read() == b""
        assert (vault / rel).read_text() == content
    assert _reasons(caplog) == ["not_knowledge"] * 3


async def test_a_note_marked_lens_in_a_knowledge_folder_is_not_writable_either(client, vault: Path, caplog):
    """It resolves to knowledge (the folder is stricter), but the user
    asked for lens: a Claude write must not edit it, or keep the mark
    on content that would become lens with one settings edit."""
    content = note("lens")
    write(vault, "Library/Marked.md", content)
    with caplog.at_level(logging.INFO):
        resp = await _put(client, "Library/Marked.md", content + "More.\n", sha(content))
    assert resp.status == 403
    assert (vault / "Library/Marked.md").read_text() == content
    assert _reasons(caplog) == ["not_knowledge"]


async def test_creating_in_a_lens_folder_is_refused(client, vault: Path, caplog):
    with caplog.at_level(logging.INFO):
        plain = await _put(client, "Lens/New.md", "Hello.\n", None)
        person = await _put(client, "Lens/People/New.md", note("knowledge"), None)
        deeper = await _put(client, "Lens/Sub/New.md", "Hello.\n", None)
    assert {plain.status, person.status, deeper.status} == {403}
    assert not (vault / "Lens" / "New.md").exists()
    assert not (vault / "Lens" / "People" / "New.md").exists()
    assert not (vault / "Lens" / "Sub").exists()
    assert _reasons(caplog) == ["folder_not_knowledge", "folder_not_knowledge", "folder_missing"]


async def test_new_content_may_never_carry_the_lens_mark(client, vault: Path, caplog):
    existing = note(None)
    write(vault, "Library/Open.md", existing)
    with caplog.at_level(logging.INFO):
        create = await _put(client, "Library/New.md", note("lens"), None)
        update = await _put(client, "Library/Open.md", note("lens"), sha(existing))
    assert create.status == 403
    assert update.status == 403
    assert not (vault / "Library" / "New.md").exists()
    assert (vault / "Library/Open.md").read_text() == existing
    assert _reasons(caplog) == ["content_not_knowledge", "content_not_knowledge"]


async def test_ordinary_knowledge_writes_still_work(client, vault: Path):
    resp = await _put(client, "Library/New.md", note("knowledge", "Hello.\n"), None)
    assert resp.status == 200


# -- renaming ---------------------------------------------------------------------


async def test_renaming_a_lens_note_is_refused(client, vault: Path, caplog):
    content = note(None)
    write(vault, "Lens/Cybernetics.md", content)
    with caplog.at_level(logging.INFO):
        out = await _rename(client, "Lens/Cybernetics.md", "Library/Cybernetics.md", sha(content))
        within = await _rename(client, "Lens/Cybernetics.md", "Lens/Kybernetik.md", sha(content))
    assert out.status == 403
    assert within.status == 403
    assert (vault / "Lens/Cybernetics.md").read_text() == content
    assert not (vault / "Library/Cybernetics.md").exists()
    assert _reasons(caplog) == ["not_knowledge", "not_knowledge"]


async def test_renaming_into_the_lens_is_refused(client, vault: Path, caplog):
    content = note(None)
    write(vault, "Library/Open.md", content)
    with caplog.at_level(logging.INFO):
        into = await _rename(client, "Library/Open.md", "Lens/Open.md", sha(content))
        person = await _rename(client, "Library/Open.md", "Lens/People/Open.md", sha(content))
    assert into.status == 403
    assert person.status == 403
    assert (vault / "Library/Open.md").read_text() == content
    assert not (vault / "Lens/Open.md").exists()
    assert _reasons(caplog) == ["dest_not_knowledge", "dest_not_knowledge"]


async def test_a_rename_that_would_rewrite_a_lens_note_is_refused(client, vault: Path, caplog):
    target = note(None)
    lens = note(None, "See [[Open]].\n")
    write(vault, "Library/Open.md", target)
    write(vault, "Lens/Cybernetics.md", lens)
    with caplog.at_level(logging.INFO):
        resp = await _rename(client, "Library/Open.md", "Library/Opened.md", sha(target))
    assert resp.status == 403
    assert (vault / "Lens/Cybernetics.md").read_text() == lens
    assert (vault / "Library/Open.md").exists()
    assert _reasons(caplog) == ["linked_from_non_knowledge"]


async def test_a_lens_link_differing_only_in_case_still_refuses_the_rename(client, vault: Path, caplog):
    """The graph resolves `[[open]]` to `Open.md`, as Obsidian does; the
    rename's backlink scan must see the same link, or a claude.ai rename
    would leave the lens pointing nowhere."""
    target = note(None)
    lens = note(None, "See [[open]].\n")
    write(vault, "Library/Open.md", target)
    write(vault, "Lens/Cybernetics.md", lens)
    with caplog.at_level(logging.INFO):
        resp = await _rename(client, "Library/Open.md", "Library/Opened.md", sha(target))
    assert resp.status == 403
    assert (vault / "Lens/Cybernetics.md").read_text() == lens
    assert _reasons(caplog) == ["linked_from_non_knowledge"]


async def test_a_knowledge_backlink_differing_in_case_or_in_a_table_is_rewritten(client, vault: Path):
    target = note(None)
    source = note(None, "See [[open]] and | x | [[Open\\|o]] |.\n")
    write(vault, "Library/Open.md", target)
    write(vault, "Library/Source.md", source)
    resp = await _rename(client, "Library/Open.md", "Library/Opened.md", sha(target))
    assert resp.status == 200
    assert (vault / "Library/Source.md").read_text().endswith("\nSee [[Opened]] and | x | [[Opened\\|o]] |.\n")


async def test_a_name_shared_up_to_case_makes_the_rename_ambiguous(client, vault: Path, caplog):
    target = note(None)
    write(vault, "Library/Open.md", target)
    write(vault, "Library/Deep/open.md", note(None))
    with caplog.at_level(logging.INFO):
        resp = await _rename(client, "Library/Open.md", "Library/Opened.md", sha(target))
    assert resp.status == 403
    assert _reasons(caplog) == ["ambiguous_basename"]
