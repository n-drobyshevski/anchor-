"""`PUT /v1/knowledge` and `GET /v1/knowledge`: the class boundary (write-plan section 4)."""

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


async def _put(client, path: str, content: str, if_sha256: str | None, changeset: str = "cs"):
    return await client.put(
        "/v1/knowledge",
        params={"path": path},
        json={"content": content, "if_sha256": if_sha256, "changeset": changeset},
        headers=AUTH,
    )


@pytest.fixture(autouse=True)
def _base_settings(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    (vault / "Library").mkdir(exist_ok=True)
    (vault / "Life").mkdir(exist_ok=True)
    (vault / "Life" / "Diary").mkdir(exist_ok=True)
    (vault / "Elsewhere").mkdir(exist_ok=True)


# -- creating -----------------------------------------------------------------


async def test_create_in_a_knowledge_folder(client, vault: Path):
    resp = await _put(client, "Library/CCRU.md", "Hello.\n", None)
    assert resp.status == 200
    body = (vault / "Library" / "CCRU.md").read_text()
    assert body.endswith("Hello.\n")
    assert "anchor_edited_by: claude" in body


async def test_create_with_explicit_anchor_knowledge_frontmatter_is_accepted(client, vault: Path):
    resp = await _put(client, "Library/CCRU.md", note("knowledge", "Hello.\n"), None)
    assert resp.status == 200
    assert (vault / "Library" / "CCRU.md").exists()


async def test_create_own_frontmatter_says_personal_is_refused(client, vault: Path):
    resp = await _put(client, "Library/CCRU.md", note("personal"), None)
    assert resp.status == 403
    assert await resp.read() == b""
    assert not (vault / "Library" / "CCRU.md").exists()


async def test_create_in_a_non_knowledge_folder_is_refused(client, vault: Path):
    resp = await _put(client, "Elsewhere/X.md", "Hello.\n", None)
    assert resp.status == 403
    assert not (vault / "Elsewhere" / "X.md").exists()


async def test_create_in_a_folder_that_does_not_exist_is_refused(client, vault: Path):
    resp = await _put(client, "Library/Sub/X.md", "Hello.\n", None)
    assert resp.status == 403
    assert not (vault / "Library" / "Sub").exists()


async def test_create_where_the_name_is_taken_is_refused(client, vault: Path):
    write(vault, "Library/CCRU.md", note("knowledge"))
    resp = await _put(client, "Library/CCRU.md", "New body.\n", None)
    assert resp.status == 403
    assert (vault / "Library" / "CCRU.md").read_text() == note("knowledge")


# -- updating: the existing file's class ---------------------------------------


async def test_update_of_a_personal_note_is_refused(client, vault: Path):
    write(vault, "Life/Партнёр.md", note("knowledge"))  # a knowledge property in a personal folder: personal wins
    resp = await _put(client, "Life/Партнёр.md", "New.\n", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Life" / "Партнёр.md").read_text() == note("knowledge")


async def test_update_of_a_never_note_is_refused(client, vault: Path):
    write(vault, "Life/Diary/day.md", note("knowledge"))
    resp = await _put(client, "Life/Diary/day.md", "New.\n", sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Life" / "Diary" / "day.md").read_text() == note("knowledge")


async def test_update_of_an_unclassified_note_is_refused(client, vault: Path):
    write(vault, "Elsewhere/x.md", note(None))
    resp = await _put(client, "Elsewhere/x.md", "New.\n", sha(note(None)))
    assert resp.status == 403
    assert (vault / "Elsewhere" / "x.md").read_text() == note(None)


async def test_a_knowledge_folder_note_marked_personal_is_refused(client, vault: Path):
    write(vault, "Library/Diary.md", note("personal"))
    resp = await _put(client, "Library/Diary.md", "New.\n", sha(note("personal")))
    assert resp.status == 403
    assert (vault / "Library" / "Diary.md").read_text() == note("personal")


async def test_invalid_settings_refuses_every_write(client, vault: Path):
    write(vault, "Anchor/settings.md", "---\nanchor: settings\nbogus: 1\n---\n")
    write(vault, "Library/CCRU.md", note("knowledge"))
    resp = await _put(client, "Library/CCRU.md", "New.\n", sha(note("knowledge")))
    assert resp.status == 403
    resp2 = await _put(client, "Library/New.md", "x", None)
    assert resp2.status == 403


# -- path rules -----------------------------------------------------------------


REFUSED_PATHS = [
    "Anchor/Memory/x.md",
    "Anchor/settings.md",
    "Anchor/x.md",
    "Library/x.txt",
    "Library/x.canvas",
    "Library/./x.md",
    "Library/../x.md",
    "Library/.hidden.md",
]


@pytest.mark.parametrize("path", REFUSED_PATHS)
async def test_bad_paths_are_refused(client, vault: Path, path: str):
    resp = await _put(client, path, "x", None)
    assert resp.status in (400, 403)


async def test_anchor_is_refused_even_if_settings_mistakenly_calls_it_knowledge(client, vault: Path):
    """Defence in depth: the folder-rule check alone would also catch this
    (Anchor is never a real knowledge folder), but `is_candidate_path`'s
    own `Anchor/` exclusion must refuse it independently."""
    write(vault, "Anchor/settings.md", "---\nanchor: settings\nknowledge_folders: [Anchor]\n---\n")
    resp = await _put(client, "Anchor/New.md", "x", None)
    assert resp.status == 403
    assert not (vault / "Anchor" / "New.md").exists()


async def test_a_symlinked_folder_is_refused(client, vault: Path, tmp_path: Path):
    import os

    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, vault / "Library" / "Link")
    resp = await _put(client, "Library/Link/x.md", "x", None)
    assert resp.status == 403
    assert list(outside.iterdir()) == []


async def test_a_symlinked_file_update_is_refused(client, vault: Path, tmp_path: Path):
    import os

    secret = tmp_path / "secret.md"
    secret.write_text(note("knowledge"))
    os.symlink(secret, vault / "Library" / "link.md")
    resp = await _put(client, "Library/link.md", "New.\n", sha(secret.read_bytes()))
    assert resp.status in (403, 404, 412)
    assert secret.read_text() == note("knowledge")


# -- frontmatter cannot reclassify ------------------------------------------------


async def test_a_write_changing_anchor_property_is_refused(client, vault: Path):
    write(vault, "Library/CCRU.md", note("knowledge"))
    resp = await _put(client, "Library/CCRU.md", note("personal", "New body.\n"), sha(note("knowledge")))
    assert resp.status == 403
    assert (vault / "Library" / "CCRU.md").read_text() == note("knowledge")


async def test_a_write_adding_anchor_when_absent_is_refused(client, vault: Path):
    write(vault, "Library/CCRU.md", "Plain body.\n")
    resp = await _put(client, "Library/CCRU.md", note("knowledge", "New.\n"), sha(b"Plain body.\n"))
    assert resp.status == 403
    assert (vault / "Library" / "CCRU.md").read_text() == "Plain body.\n"


async def test_a_write_keeping_anchor_knowledge_is_accepted(client, vault: Path):
    write(vault, "Library/CCRU.md", note("knowledge"))
    resp = await _put(client, "Library/CCRU.md", note("knowledge", "New.\n"), sha(note("knowledge")))
    assert resp.status == 200


async def test_a_write_with_no_anchor_property_either_side_is_accepted(client, vault: Path):
    write(vault, "Library/CCRU.md", "Plain body.\n")
    resp = await _put(client, "Library/CCRU.md", "New body.\n", sha(b"Plain body.\n"))
    assert resp.status == 200


# -- size, utf-8, frontmatter parsing ---------------------------------------------


async def test_oversize_content_is_refused(client, vault: Path):
    resp = await _put(client, "Library/CCRU.md", "x" * 65537, None)
    assert resp.status == 403
    assert not (vault / "Library" / "CCRU.md").exists()


async def test_content_at_the_cap_is_accepted(client, vault: Path):
    resp = await _put(client, "Library/CCRU.md", "x" * 65536, None)
    assert resp.status == 200


async def test_bad_utf8_content_is_refused(client, vault: Path):
    resp = await client.put(
        "/v1/knowledge",
        params={"path": "Library/CCRU.md"},
        json={"content": "\ud800", "if_sha256": None, "changeset": "cs"},
        headers=AUTH,
    )
    assert resp.status == 403
    assert not (vault / "Library" / "CCRU.md").exists()


async def test_unparsable_frontmatter_is_refused(client, vault: Path):
    resp = await _put(client, "Library/CCRU.md", "---\nanchor: [oops\n---\nBody.\n", None)
    assert resp.status == 403


# -- CAS -------------------------------------------------------------------------


async def test_cas_mismatch_is_412(client, vault: Path):
    write(vault, "Library/CCRU.md", note("knowledge"))
    resp = await _put(client, "Library/CCRU.md", "New.\n", "0" * 64)
    assert resp.status == 412
    assert (vault / "Library" / "CCRU.md").read_text() == note("knowledge")


async def test_update_of_a_missing_file_is_404(client, vault: Path):
    resp = await _put(client, "Library/Missing.md", "New.\n", "0" * 64)
    assert resp.status == 404
    body = await resp.json()
    assert body == {"error": "not_found"}


# -- every 403 refusal is byte-identical -----------------------------------------


async def test_every_403_is_byte_identical(client, vault: Path):
    write(vault, "Library/CCRU.md", note("knowledge"))
    write(vault, "Life/Диван.md", note("personal"))
    scenarios = [
        _put(client, "Anchor/x.md", "x", None),
        _put(client, "Elsewhere/x.md", "x", None),
        _put(client, "Library/Sub/x.md", "x", None),
        _put(client, "Life/Диван.md", "New.\n", sha(note("personal"))),
        _put(client, "Library/CCRU.md", note("personal", "New.\n"), sha(note("knowledge"))),
        _put(client, "Library/CCRU.md", "x" * 65537, None, changeset="cs-oversize"),
    ]
    bodies = set()
    for coro in scenarios:
        resp = await coro
        assert resp.status == 403
        bodies.add(await resp.read())
    assert bodies == {b""}


# -- provenance --------------------------------------------------------------------


async def test_provenance_is_set_on_create(client, vault: Path, clock):
    resp = await _put(client, "Library/CCRU.md", "Hello.\n", None)
    assert resp.status == 200
    body = (vault / "Library" / "CCRU.md").read_text()
    assert "anchor_edited_by: claude\n" in body
    assert f'anchor_edited_at: "{clock().strftime("%Y-%m-%dT%H:%M:%SZ")}"' in body


async def test_incoming_anchor_edited_keys_are_stripped(client, vault: Path):
    resp = await _put(
        client,
        "Library/CCRU.md",
        '---\nanchor_edited_by: someone-else\nanchor_edited_at: "2020-01-01T00:00:00Z"\ntitle: CCRU\n---\nHello.\n',
        None,
    )
    assert resp.status == 200
    body = (vault / "Library" / "CCRU.md").read_text()
    assert body.count("anchor_edited_by:") == 1
    assert body.count("anchor_edited_at:") == 1
    assert "someone-else" not in body
    assert "2020-01-01" not in body
    assert "title: CCRU" in body


async def test_other_properties_are_kept_byte_for_byte(client, vault: Path):
    original = "---\ntitle: CCRU\ntags:\n  - occult\n  - theory\n---\nOld.\n"
    write(vault, "Library/CCRU.md", original)
    resp = await _put(client, "Library/CCRU.md", original.replace("Old.", "New."), sha(original.encode()))
    assert resp.status == 200
    body = (vault / "Library" / "CCRU.md").read_text()
    assert "title: CCRU\n" in body
    assert "  - occult\n" in body
    assert "  - theory\n" in body
    assert body.endswith("New.\n")


async def test_provenance_on_a_file_with_no_frontmatter(client, vault: Path):
    resp = await _put(client, "Library/CCRU.md", "Just a body.\n", None)
    assert resp.status == 200
    body = (vault / "Library" / "CCRU.md").read_text()
    assert body.startswith("---\nanchor_edited_by: claude\n")
    assert body.endswith("---\nJust a body.\n")


# -- GET /v1/knowledge --------------------------------------------------------------


async def test_get_knowledge_returns_content_and_hash(client, vault: Path):
    write(vault, "Library/CCRU.md", note("knowledge"))
    resp = await client.get("/v1/knowledge", params={"path": "Library/CCRU.md"}, headers=AUTH)
    assert resp.status == 200
    body = await resp.json()
    assert body["content"] == note("knowledge")
    assert body["sha256"] == sha(note("knowledge"))


@pytest.mark.parametrize(
    "setup",
    ["personal", "never", "unclassified", "missing", "anchor_scope", "settings"],
)
async def test_get_knowledge_refuses_everything_else_with_the_same_404(client, vault: Path, setup: str):
    write(vault, "Life/personal.md", note("personal"))
    write(vault, "Life/Diary/never.md", note("knowledge"))  # folder rule makes it never
    write(vault, "Elsewhere/unclassified.md", note(None))
    write(vault, "Anchor/Memory/0001-a.md", "fact")

    paths = {
        "personal": "Life/personal.md",
        "never": "Life/Diary/never.md",
        "unclassified": "Elsewhere/unclassified.md",
        "missing": "Library/missing.md",
        "anchor_scope": "Anchor/Memory/0001-a.md",
        "settings": "Anchor/settings.md",
    }
    resp = await client.get("/v1/knowledge", params={"path": paths[setup]}, headers=AUTH)
    missing = await client.get("/v1/knowledge", params={"path": "Library/definitely-missing.md"}, headers=AUTH)
    assert resp.status == 404
    assert await resp.text() == await missing.text()
