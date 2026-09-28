"""`GET /v1/knowledge/tree` (write-plan rev. 3, section 14, BUILD item 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vaultd.config import TREE_MAX_NOTES
from tests.conftest import AUTH, write

SETTINGS = (
    "---\nanchor: settings\n"
    "knowledge_folders: [Library]\npersonal_folders: [Life]\nnever_folders: [Life/Diary]\n---\n"
)


def note(mark: str = "knowledge", body: str = "Body.\n") -> str:
    return f"---\nanchor: {mark}\n---\n{body}"


@pytest.fixture(autouse=True)
def _base_settings(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    (vault / "Library").mkdir(exist_ok=True)
    (vault / "Life").mkdir(exist_ok=True)
    (vault / "Life" / "Diary").mkdir(exist_ok=True)


async def _tree(client) -> dict:
    resp = await client.get("/v1/knowledge/tree", headers=AUTH)
    assert resp.status == 200
    return await resp.json()


async def test_lists_only_knowledge_folders_and_notes(client, vault: Path):
    write(vault, "Library/CCRU.md", note())
    (vault / "Library" / "Philosophy").mkdir()
    write(vault, "Library/Philosophy/Stoicism.md", note())
    write(vault, "Life/Дневник.md", note("personal"))

    tree = await _tree(client)
    assert set(tree["folders"]) == {"Library", "Library/Philosophy"}
    assert {n["path"] for n in tree["notes"]} == {"Library/CCRU.md", "Library/Philosophy/Stoicism.md"}
    titles = {n["title"] for n in tree["notes"]}
    assert titles == {"CCRU", "Stoicism"}
    assert tree["truncated"] is False


async def test_hides_a_personal_marked_note_inside_a_knowledge_folder(client, vault: Path):
    write(vault, "Library/Open.md", note("knowledge"))
    write(vault, "Library/Diary.md", note("personal"))

    tree = await _tree(client)
    paths = {n["path"] for n in tree["notes"]}
    assert "Library/Open.md" in paths
    assert "Library/Diary.md" not in paths


async def test_hides_everything_under_anchor(client, vault: Path):
    write(vault, "Anchor/Memory/0001-a.md", "fact\n")
    write(vault, "Library/Open.md", note("knowledge"))

    tree = await _tree(client)
    assert not any(f.startswith("Anchor") for f in tree["folders"])
    assert not any(n["path"].startswith("Anchor") for n in tree["notes"])


async def test_hides_anchor_even_if_settings_mistakenly_calls_it_knowledge(client, vault: Path):
    """Defense in depth, the same shape as test_knowledge_write.py's own
    `test_anchor_is_refused_even_if_settings_mistakenly_calls_it_knowledge`:
    `Anchor/` is never listed even if `knowledge_folders` perversely
    names it and the note's own property says `knowledge` -- isolates
    `build_tree`'s own `Anchor/` exclusion from the ordinary
    effective-class filter, which a real settings file would already
    catch on its own (Anchor is never a real knowledge folder)."""
    write(
        vault,
        "Anchor/settings.md",
        "---\nanchor: settings\nknowledge_folders: [Anchor, Library]\n---\n",
    )
    write(vault, "Anchor/Memory/0001-a.md", note("knowledge"))

    tree = await _tree(client)
    assert not any(f.startswith("Anchor") for f in tree["folders"])
    assert not any(n["path"].startswith("Anchor") for n in tree["notes"])


async def test_hides_a_note_outside_any_knowledge_folder(client, vault: Path):
    write(vault, "Elsewhere/x.md", note(None))
    tree = await _tree(client)
    assert tree["notes"] == []
    # "Library" itself is still listed: it is a knowledge_folders root
    # that exists on disk, just empty; "Elsewhere" never appears.
    assert tree["folders"] == ["Library"]


async def test_truncates_at_the_cap(client, vault: Path):
    assert TREE_MAX_NOTES == 2000
    for i in range(TREE_MAX_NOTES + 3):
        write(vault, f"Library/N{i:04d}.md", note())

    tree = await _tree(client)
    assert len(tree["notes"]) == TREE_MAX_NOTES
    assert tree["truncated"] is True


async def test_empty_on_invalid_settings(client, vault: Path):
    write(vault, "Anchor/settings.md", "---\nanchor: settings\nbogus_key: [1]\n---\n")
    write(vault, "Library/Open.md", note())

    tree = await _tree(client)
    assert tree == {"folders": [], "notes": [], "truncated": False}


async def test_no_file_content_in_the_response(client, vault: Path):
    secret = "совершенно секретный текст"
    write(vault, "Library/CCRU.md", note("knowledge", secret + "\n"))

    resp = await client.get("/v1/knowledge/tree", headers=AUTH)
    raw = await resp.text()
    assert secret not in raw
