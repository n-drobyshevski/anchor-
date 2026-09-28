"""Folder auto-creation on the way to a new note or a move target
(write-plan rev. 3, anchor-claude-write-plan.md section 14, BUILD item
1-2). Every refusal proven with its own test (project instructions)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from vaultd.config import FOLDER_MAX_DEPTH, FOLDERS_PER_CHANGESET, FOLDERS_PER_DAY
from tests.conftest import AUTH, FakeClock, write

SETTINGS = (
    "---\nanchor: settings\n"
    "knowledge_folders: [Library]\npersonal_folders: [Life]\nnever_folders: [Life/Diary]\n---\n"
)


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def note(mark: str | None = "knowledge", body: str = "Body.\n") -> str:
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


async def _put(client, path: str, content: str, if_sha256: str | None, changeset: str = "cs"):
    return await client.put(
        "/v1/knowledge",
        params={"path": path},
        json={"content": content, "if_sha256": if_sha256, "changeset": changeset},
        headers=AUTH,
    )


# -- creates nested folders under a knowledge root --------------------------


async def test_creates_nested_folders_under_a_knowledge_root(client, vault: Path):
    resp = await _put(client, "Library/Philosophy/Stoicism/Seneca.md", "Hello.\n", None)
    assert resp.status == 200
    body = await resp.json()
    assert body["folders_created"] == 2
    assert (vault / "Library" / "Philosophy" / "Stoicism" / "Seneca.md").read_text().endswith("Hello.\n")
    assert (vault / "Library" / "Philosophy").is_dir()
    assert (vault / "Library" / "Philosophy" / "Stoicism").is_dir()


async def test_an_existing_folder_creates_nothing(client, vault: Path):
    (vault / "Library" / "Existing").mkdir()
    resp = await _put(client, "Library/Existing/New.md", "Hello.\n", None)
    assert resp.status == 200
    body = await resp.json()
    assert body["folders_created"] == 0


# -- refused outside any knowledge root (top-level) --------------------------


async def test_refused_outside_any_knowledge_root(client, vault: Path):
    resp = await _put(client, "Elsewhere/Sub/New.md", "Hello.\n", None)
    assert resp.status == 403
    assert not (vault / "Elsewhere" / "Sub").exists()


async def test_never_creates_a_top_level_folder(client, vault: Path):
    """Even when settings *names* a top-level folder as a knowledge
    root, vaultd never creates that root itself -- only builds further
    from an ancestor that already exists on disk."""
    write(vault, "Anchor/settings.md", "---\nanchor: settings\nknowledge_folders: [Brand-New]\n---\n")
    resp = await _put(client, "Brand-New/X.md", "Hello.\n", None)
    assert resp.status == 403
    assert not (vault / "Brand-New").exists()


# -- refused beyond depth 4 --------------------------------------------------


async def test_refused_beyond_max_depth(client, vault: Path):
    assert FOLDER_MAX_DEPTH == 4
    too_deep = "Library/" + "/".join(f"L{i}" for i in range(FOLDER_MAX_DEPTH + 1)) + "/X.md"
    resp = await _put(client, too_deep, "x", None)
    assert resp.status == 403
    assert not (vault / "Library" / "L0").exists()


async def test_accepted_at_max_depth(client, vault: Path):
    # The first two levels already exist, so this call only has to
    # create the remaining two -- FOLDERS_PER_CHANGESET (3) would
    # otherwise refuse a single call that creates all four levels at
    # once, which is a different cap than the depth one this test is
    # isolating.
    (vault / "Library" / "L0" / "L1").mkdir(parents=True)
    at_cap = "Library/" + "/".join(f"L{i}" for i in range(FOLDER_MAX_DEPTH)) + "/X.md"
    resp = await _put(client, at_cap, "x", None)
    assert resp.status == 200
    body = await resp.json()
    assert body["folders_created"] == 2


# -- refused when a never/personal rule covers the new path -----------------


async def test_refused_when_a_never_rule_covers_the_new_path(client, vault: Path):
    write(
        vault,
        "Anchor/settings.md",
        "---\nanchor: settings\nknowledge_folders: [Library]\nnever_folders: [Library/Secret]\n---\n",
    )
    resp = await _put(client, "Library/Secret/New.md", "x", None)
    assert resp.status == 403
    assert not (vault / "Library" / "Secret").exists()


async def test_refused_when_a_personal_rule_covers_the_new_path(client, vault: Path):
    write(
        vault,
        "Anchor/settings.md",
        "---\nanchor: settings\nknowledge_folders: [Library]\npersonal_folders: [Library/Private]\n---\n",
    )
    resp = await _put(client, "Library/Private/New.md", "x", None)
    assert resp.status == 403
    assert not (vault / "Library" / "Private").exists()


# -- refused on bad segment names --------------------------------------------


@pytest.mark.parametrize("segment", [".hidden", "x" * 121])
async def test_refused_on_a_bad_segment_name(client, vault: Path, segment: str):
    resp = await _put(client, f"Library/{segment}/New.md", "x", None)
    assert resp.status == 403
    assert not (vault / "Library" / segment).exists()


async def test_refused_on_a_control_character_in_a_segment_name(client, vault: Path):
    resp = await _put(client, "Library/bad\x01name/New.md", "x", None)
    assert resp.status == 403
    assert list((vault / "Library").iterdir()) == []


def test_plan_new_folders_itself_refuses_a_leading_dot_or_empty_segment(vault: Path):
    """`plan_new_folders` is called directly here, bypassing
    `candidate_path_reason`'s own dot-segment check and `paths.parse_rel`'s
    own empty-segment check, both of which the HTTP path already applies
    to `rel` before this function is ever reached -- defense in depth,
    the same shape as test_knowledge_write.py's
    `test_anchor_is_refused_even_if_settings_mistakenly_calls_it_knowledge`."""
    from vaultd import classes, knowledge

    rules = classes.parse_settings(SETTINGS.encode())
    with pytest.raises(knowledge.Refused) as leading_dot:
        knowledge.plan_new_folders(vault, "Library/.hidden/New.md", rules)
    assert leading_dot.value.reason == "folder_name_bad"
    with pytest.raises(knowledge.Refused) as empty:
        knowledge.plan_new_folders(vault, "Library//New.md", rules)
    assert empty.value.reason == "folder_name_bad"


def test_plan_new_folders_itself_refuses_a_segment_holding_a_slash_or_backslash(vault: Path):
    """Defense in depth, called directly: a literal '/' cannot survive
    `rel.split('/')` into one segment through the HTTP path, and a
    literal '\\\\' is already refused earlier by `paths.parse_rel`
    (test_paths.py) -- `_safe_segment`'s own copy of both checks is
    proven here, the same way as the dot/empty case above."""
    from vaultd import knowledge

    assert knowledge._safe_segment("a/b") is False  # noqa: SLF001 - same package
    assert knowledge._safe_segment("a\\b") is False  # noqa: SLF001




async def test_refused_on_a_non_nfc_segment_name(client, vault: Path):
    decomposed = "élan"  # "e" + combining acute accent, not NFC
    resp = await _put(client, f"Library/{decomposed}/New.md", "x", None)
    assert resp.status == 403
    assert list((vault / "Library").iterdir()) == []


# -- refused through a symlink -----------------------------------------------


async def test_refused_through_a_symlinked_ancestor(client, vault: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, vault / "Library" / "Link")
    resp = await _put(client, "Library/Link/New/X.md", "x", None)
    assert resp.status == 403
    assert list(outside.iterdir()) == []


# -- folder caps: per changeset and per day ----------------------------------


async def test_folder_cap_per_changeset(client, vault: Path):
    assert FOLDERS_PER_CHANGESET == 3
    resp = await _put(
        client,
        "Library/" + "/".join(f"L{i}" for i in range(FOLDERS_PER_CHANGESET)) + "/X.md",
        "x",
        None,
        changeset="cs",
    )
    assert resp.status == 200
    resp2 = await _put(client, "Library/M0/M1/Y.md", "x", None, changeset="cs")
    assert resp2.status == 403
    assert not (vault / "Library" / "M0").exists()


async def test_folder_cap_per_day(client, vault: Path, clock: FakeClock):
    assert FOLDERS_PER_DAY == 10
    for i in range(FOLDERS_PER_DAY):
        resp = await _put(client, f"Library/D{i}/X.md", "x", None, changeset=f"cs{i}")
        assert resp.status == 200, i
        clock.advance(hours=1, minutes=5)
    resp = await _put(client, "Library/Overflow/X.md", "x", None, changeset="cs-over")
    assert resp.status == 403
    assert not (vault / "Library" / "Overflow").exists()


# -- undo removes created empty folders, deepest first ----------------------


async def test_undo_removes_created_empty_folders_deepest_first(client, vault: Path):
    resp = await _put(client, "Library/A/B/New.md", "Hello.\n", None, changeset="cs")
    assert resp.status == 200

    undo = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert (await undo.json())["restored"] == 1
    assert not (vault / "Library" / "A" / "B" / "New.md").exists()
    assert not (vault / "Library" / "A" / "B").exists()
    assert not (vault / "Library" / "A").exists()


async def test_undo_leaves_a_non_empty_created_folder(client, vault: Path):
    resp = await _put(client, "Library/A/New.md", "Hello.\n", None, changeset="cs")
    assert resp.status == 200
    write(vault, "Library/A/Other.md", "not Claude's, left behind on purpose\n")

    undo = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert (await undo.json())["restored"] == 1
    assert not (vault / "Library" / "A" / "New.md").exists()
    assert (vault / "Library" / "A").is_dir()
    assert (vault / "Library" / "A" / "Other.md").exists()


# -- reason codes: logged, bodies unchanged, no path in logs -----------------


async def test_folder_reason_codes_are_logged_with_unchanged_bodies_and_no_path(client, vault: Path, caplog):
    import logging

    from vaultd.log import JsonFormatter

    secret = "Совершенно-Секретно"
    with caplog.at_level(logging.DEBUG):
        r1 = await _put(client, f"Elsewhere/{secret}/X.md", "x", None, changeset="r1")
        r2 = await _put(
            client,
            f"Library/{'/'.join(f'L{i}' for i in range(FOLDER_MAX_DEPTH + 1))}/{secret}.md",
            "x",
            None,
            changeset="r2",
        )
        write(
            vault,
            "Anchor/settings.md",
            "---\nanchor: settings\nknowledge_folders: [Library]\nnever_folders: [Library/Hidden]\n---\n",
        )
        r3 = await _put(client, f"Library/Hidden/{secret}.md", "x", None, changeset="r3")
        r4 = await _put(client, "Library/" + "x" * 121 + "/X.md", "x", None, changeset="r4")

    for resp in (r1, r2, r3, r4):
        assert resp.status == 403
        assert await resp.read() == b""

    reasons = [
        r.__dict__["reason"]
        for r in caplog.records
        if r.__dict__.get("event") == "knowledge_refused"
    ]
    assert reasons == ["folder_missing", "folder_too_deep", "folder_not_under_knowledge", "folder_name_bad"]

    formatter = JsonFormatter()
    blob = "\n".join(
        part for r in caplog.records for part in (r.getMessage(), str(r.__dict__), formatter.format(r))
    )
    assert secret not in blob
