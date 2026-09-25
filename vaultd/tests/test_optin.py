"""The opt-in rule (plan sections 4.3, 4.4, 15): `anchor: read` and nothing else."""

from __future__ import annotations

from pathlib import Path

import pytest

from vaultd import frontmatter
from vaultd.config import FRONTMATTER_MAX_BYTES, NOTE_MAX_BYTES
from tests.conftest import AUTH, write

OPTED_IN = "---\nanchor: read\ntitle: Бег\n---\nБегаю по утрам.\n"

NOT_OPTED_IN = {
    "other value": "---\nanchor: fact\n---\nbody\n",
    "capitalised value": "---\nanchor: Read\n---\nbody\n",
    "list value": "---\nanchor: [read]\n---\nbody\n",
    "bool-ish value": "---\nanchor: yes\n---\nbody\n",
    "no frontmatter": "anchor: read\n\nbody\n",
    "only a tag": "---\ntags: [anchor]\n---\nbody\n",
    "unclosed fence": "---\nanchor: read\nbody\n",
    "fence not at the start": "\n---\nanchor: read\n---\nbody\n",
    "byte-order mark": "﻿---\nanchor: read\n---\nbody\n",
    "malformed yaml": "---\nanchor: read\n  : : [\n---\nbody\n",
    "top level is a list": "---\n- anchor: read\n---\nbody\n",
    "alias bomb": (
        "---\na: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
        "c: [*b, *b, *b, *b, *b, *b, *b, *b, *b]\nanchor: read\n---\nbody\n"
    ),
    "anchor without alias": "---\nanchor: &x read\n---\nbody\n",
    "duplicate keys": "---\nanchor: read\nanchor: read\n---\nbody\n",
    "duplicate keys, different values": "---\nanchor: fact\nanchor: read\n---\nbody\n",
    "nested duplicate keys": "---\nanchor: read\nmeta:\n  a: 1\n  a: 2\n---\nbody\n",
    "python tag": "---\nanchor: !!python/name:os.system read\n---\nbody\n",
    "frontmatter over 4 KB": "---\nanchor: read\nfiller: \"" + "x" * FRONTMATTER_MAX_BYTES + "\"\n---\nbody\n",
}


def test_opted_in_note_passes() -> None:
    assert frontmatter.is_opted_in(OPTED_IN.encode())
    assert frontmatter.is_opted_in(b"---\r\nanchor: read\r\n---\r\nbody\r\n")
    assert frontmatter.is_opted_in(b'---\nanchor: "read"\n---\n')


@pytest.mark.parametrize("name", sorted(NOT_OPTED_IN))
def test_not_opted_in(name: str) -> None:
    assert not frontmatter.is_opted_in(NOT_OPTED_IN[name].encode())


def test_invalid_utf8_is_not_opted_in() -> None:
    assert not frontmatter.is_opted_in(b"---\nanchor: read\n---\n\xff\xfe body\n")


async def test_manifest_lists_only_opted_in_notes(client, vault: Path) -> None:
    write(vault, "Бег.md", OPTED_IN)
    for i, name in enumerate(sorted(NOT_OPTED_IN)):
        write(vault, f"not-{i}.md", NOT_OPTED_IN[name])
    write(vault, "big.md", "---\nanchor: read\n---\n" + "x" * NOTE_MAX_BYTES)
    write(vault, "picture.png", "---\nanchor: read\n---\n")
    manifest = await (await client.get("/v1/manifest", headers=AUTH)).json()
    assert manifest["files"] == [
        {
            "path": "Бег.md",
            "sha256": manifest["files"][0]["sha256"],
            "size": len(OPTED_IN.encode()),
            "scope": "note",
        }
    ]


async def test_not_opted_in_is_the_same_404_as_missing(client, vault: Path) -> None:
    write(vault, "private.md", "---\nanchor: fact\n---\nмой дневник\n")
    write(vault, "big.md", "---\nanchor: read\n---\n" + "x" * NOTE_MAX_BYTES)
    missing = await client.get("/v1/file", params={"path": "nope.md"}, headers=AUTH)
    private = await client.get("/v1/file", params={"path": "private.md"}, headers=AUTH)
    big = await client.get("/v1/file", params={"path": "big.md"}, headers=AUTH)
    folder = await client.get("/v1/file", params={"path": "Anchor"}, headers=AUTH)
    bodies = {await r.text() for r in (missing, private, big, folder)}
    assert {missing.status, private.status, big.status, folder.status} == {404}
    assert len(bodies) == 1


async def test_opted_in_note_can_be_read_and_the_opt_in_is_checked_at_read_time(
    client, vault: Path
) -> None:
    write(vault, "Бег.md", OPTED_IN)
    got = await client.get("/v1/file", params={"path": "Бег.md"}, headers=AUTH)
    assert got.status == 200
    assert (await got.json())["content"] == OPTED_IN
    write(vault, "Бег.md", "---\nanchor: no\n---\nБегаю по утрам.\n")
    again = await client.get("/v1/file", params={"path": "Бег.md"}, headers=AUTH)
    assert again.status == 404


async def test_notes_are_never_writable(client, vault: Path) -> None:
    write(vault, "Бег.md", OPTED_IN)
    resp = await client.put(
        "/v1/file", params={"path": "Бег.md"}, json={"content": "x", "if_sha256": None}, headers=AUTH
    )
    assert resp.status == 403
    assert (vault / "Бег.md").read_text() == OPTED_IN
