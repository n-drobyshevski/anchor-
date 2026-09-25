"""A note's own mark (8e plan sections 3-4; phase-8 plan 4.4, 15).

Exactly `anchor: never|personal|knowledge`, and 8a's `anchor: read` as
`legacy_read`. Properties that cannot be read are `unknown`, never
`none`: `none` would let a folder rule reveal the note.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vaultd import frontmatter
from vaultd.config import FRONTMATTER_MAX_BYTES, NOTE_MAX_BYTES
from tests.conftest import AUTH, write

READABLE = "---\nanchor: personal\ntitle: Бег\n---\nБегаю по утрам.\n"

MARKS = {
    "never": ("---\nanchor: never\n---\nbody\n", "never"),
    "personal": ("---\nanchor: personal\n---\nbody\n", "personal"),
    "knowledge": ("---\nanchor: knowledge\n---\nbody\n", "knowledge"),
    "legacy read": ("---\nanchor: read\n---\nbody\n", "legacy_read"),
    "quoted": ('---\nanchor: "knowledge"\n---\n', "knowledge"),
    "crlf": ("---\r\nanchor: personal\r\n---\r\nbody\r\n", "personal"),
    # none: the note says nothing, so a folder rule may decide.
    "no frontmatter": ("anchor: knowledge\n\nbody\n", "none"),
    "fence not at the start": ("\n---\nanchor: knowledge\n---\nbody\n", "none"),
    "only a tag": ("---\ntags: [anchor]\n---\nbody\n", "none"),
    "empty frontmatter": ("---\n---\nbody\n", "none"),
    "empty file": ("", "none"),
    # unknown: a value Anchor does not know.
    "other value": ("---\nanchor: fact\n---\nbody\n", "unknown"),
    "settings value": ("---\nanchor: settings\n---\nbody\n", "unknown"),
    "capitalised value": ("---\nanchor: Knowledge\n---\nbody\n", "unknown"),
    "capitalised read": ("---\nanchor: Read\n---\nbody\n", "unknown"),
    "typo": ("---\nanchor: knowlege\n---\nbody\n", "unknown"),
    "list value": ("---\nanchor: [knowledge]\n---\nbody\n", "unknown"),
    "bool-ish value": ("---\nanchor: yes\n---\nbody\n", "unknown"),
    "null value": ("---\nanchor:\n---\nbody\n", "unknown"),
    # unknown: properties that cannot be read at all.
    "unclosed fence": ("---\nanchor: never\nbody\n", "unknown"),
    "byte-order mark": ("﻿---\nanchor: knowledge\n---\nbody\n", "unknown"),
    "malformed yaml": ("---\nanchor: knowledge\n  : : [\n---\nbody\n", "unknown"),
    "top level is a list": ("---\n- anchor: knowledge\n---\nbody\n", "unknown"),
    "alias bomb": (
        "---\na: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
        "c: [*b, *b, *b, *b, *b, *b, *b, *b, *b]\nanchor: knowledge\n---\nbody\n",
        "unknown",
    ),
    "anchor without alias": ("---\nanchor: &x knowledge\n---\nbody\n", "unknown"),
    "duplicate keys": ("---\nanchor: knowledge\nanchor: knowledge\n---\nbody\n", "unknown"),
    "duplicate keys, different values": ("---\nanchor: never\nanchor: knowledge\n---\nbody\n", "unknown"),
    "nested duplicate keys": ("---\nanchor: knowledge\nmeta:\n  a: 1\n  a: 2\n---\nbody\n", "unknown"),
    "python tag": ("---\nanchor: !!python/name:os.system knowledge\n---\nbody\n", "unknown"),
    "frontmatter over 4 KB": (
        "---\nanchor: knowledge\nfiller: \"" + "x" * FRONTMATTER_MAX_BYTES + "\"\n---\nbody\n",
        "unknown",
    ),
}


@pytest.mark.parametrize("name", sorted(MARKS))
def test_note_mark(name: str) -> None:
    text, expected = MARKS[name]
    assert frontmatter.note_mark(text.encode()) == expected


def test_invalid_utf8_is_unknown() -> None:
    assert frontmatter.note_mark(b"---\nanchor: knowledge\n---\n\xff\xfe body\n") == "unknown"
    assert frontmatter.note_mark(b"no frontmatter \xff\n") == "unknown"


async def test_manifest_lists_only_classified_notes(client, vault: Path) -> None:
    write(vault, "Бег.md", READABLE)
    for i, name in enumerate(sorted(MARKS)):
        text, expected = MARKS[name]
        if expected not in ("personal", "knowledge", "legacy_read"):
            write(vault, f"not-{i}.md", text)
    write(vault, "big.md", "---\nanchor: personal\n---\n" + "x" * NOTE_MAX_BYTES)
    write(vault, "picture.png", "---\nanchor: personal\n---\n")
    manifest = await (await client.get("/v1/manifest", headers=AUTH)).json()
    assert manifest["files"] == [
        {
            "path": "Бег.md",
            "sha256": manifest["files"][0]["sha256"],
            "size": len(READABLE.encode()),
            "scope": "note",
            "class": "personal",
        }
    ]


async def test_invisible_is_the_same_404_as_missing(client, vault: Path) -> None:
    write(vault, "private.md", "---\nanchor: fact\n---\nмой дневник\n")
    write(vault, "never.md", "---\nanchor: never\n---\nмой дневник\n")
    write(vault, "plain.md", "мой дневник\n")
    write(vault, "big.md", "---\nanchor: personal\n---\n" + "x" * NOTE_MAX_BYTES)
    paths = ("nope.md", "private.md", "never.md", "plain.md", "big.md", "Anchor")
    responses = [await client.get("/v1/file", params={"path": p}, headers=AUTH) for p in paths]
    assert {r.status for r in responses} == {404}
    assert len({await r.text() for r in responses}) == 1


async def test_a_note_can_be_read_and_its_class_is_checked_at_read_time(client, vault: Path) -> None:
    write(vault, "Бег.md", READABLE)
    got = await client.get("/v1/file", params={"path": "Бег.md"}, headers=AUTH)
    assert got.status == 200
    body = await got.json()
    assert body["content"] == READABLE
    assert body["class"] == "personal"
    write(vault, "Бег.md", "---\nanchor: knowledge\n---\nБегаю по утрам.\n")
    got = await client.get("/v1/file", params={"path": "Бег.md"}, headers=AUTH)
    assert (await got.json())["class"] == "knowledge"
    write(vault, "Бег.md", "---\nanchor: never\n---\nБегаю по утрам.\n")
    again = await client.get("/v1/file", params={"path": "Бег.md"}, headers=AUTH)
    assert again.status == 404


async def test_anchor_files_carry_no_class(client, vault: Path) -> None:
    write(vault, "Anchor/Memory/0001-abcdef.md", "---\nanchor: fact\n---\n")
    got = await client.get("/v1/file", params={"path": "Anchor/Memory/0001-abcdef.md"}, headers=AUTH)
    assert got.status == 200
    assert "class" not in await got.json()


async def test_notes_are_never_writable(client, vault: Path) -> None:
    write(vault, "Бег.md", READABLE)
    resp = await client.put(
        "/v1/file", params={"path": "Бег.md"}, json={"content": "x", "if_sha256": None}, headers=AUTH
    )
    assert resp.status == 403
    assert (vault / "Бег.md").read_text() == READABLE
