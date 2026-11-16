"""The code window's vault side (anchor-code-window-plan.md section 3).

`POST /v1/code/tree` and `POST /v1/code/file`: dark unless
VAULT_CODE_ACCESS is on; any `.md` except `never` and `Anchor/`;
every file refusal the one 404 body; an unusable settings file is 409;
logs carry no path, root or text. All data here is synthetic, in the
test's temp vault.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

import pytest

from vaultd import code
from vaultd.api import make_app
from vaultd.config import CODE_FILE_MAX_BYTES, CODE_ROOTS_MAX, from_env, switch_on
from tests.conftest import AUTH, StaticStatus, TOKEN, write
from tests.test_logs import _everything_logged

NOT_FOUND = {"error": "not_found"}
SETTINGS = (
    "---\nanchor: settings\n"
    "knowledge_folders: [Library]\npersonal_folders: [Life]\nnever_folders: [Life/Diary, Vault]\n"
    "lens_folders: [Lens]\n---\n"
)


def note(mark: str | None = None, body: str = "Body.\n") -> str:
    if mark is None:
        return body
    return f"---\nanchor: {mark}\n---\n{body}"


@pytest.fixture
async def code_client(aiohttp_client, store, manifest, undo_store, clock):
    app = make_app(
        token=TOKEN,
        store=store,
        manifest_=manifest,
        status_source=StaticStatus(),
        undo_store=undo_store,
        clock=clock,
        code_access=True,
    )
    return await aiohttp_client(app)


@pytest.fixture
def settings(vault: Path) -> None:
    write(vault, "Anchor/settings.md", SETTINGS)


async def tree(client, roots: list[str] | None = None) -> dict:
    resp = await client.post("/v1/code/tree", json={"roots": roots or []}, headers=AUTH)
    assert resp.status == 200
    return await resp.json()


async def tree_paths(client, roots: list[str] | None = None) -> list[str]:
    return [f["path"] for f in (await tree(client, roots))["files"]]


async def read(client, path: str, roots: list[str] | None = None):
    return await client.post("/v1/code/file", json={"path": path, "roots": roots or []}, headers=AUTH)


# -- the switch -----------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_the_switch_reads_truthy_values_as_on(raw: str) -> None:
    assert switch_on(raw) is True


@pytest.mark.parametrize("raw", ["", "0", "false", "off", "no", "enabled", "tru"])
def test_anything_else_leaves_the_switch_off(raw: str) -> None:
    assert switch_on(raw) is False


def test_the_switch_is_read_from_the_environment_and_defaults_off(boot_env) -> None:
    assert from_env(boot_env).code_access is False
    assert from_env({**boot_env, "VAULT_CODE_ACCESS": "true"}).code_access is True
    assert from_env({**boot_env, "VAULT_CODE_ACCESS": "nope"}).code_access is False


async def test_off_both_routes_are_the_404_of_a_route_that_does_not_exist(client, vault: Path) -> None:
    write(vault, "Notes/a.md", note())
    unknown = await client.post("/v1/nowhere", json={"roots": []}, headers=AUTH)
    expected = (unknown.status, await unknown.text())
    assert expected[0] == 404
    for url, body in (
        ("/v1/code/tree", {"roots": []}),
        ("/v1/code/file", {"path": "Notes/a.md", "roots": []}),
    ):
        resp = await client.post(url, json=body, headers=AUTH)
        assert (resp.status, await resp.text()) == expected


async def test_status_reports_the_switch(client, code_client) -> None:
    off = await (await client.get("/v1/status", headers=AUTH)).json()
    on = await (await code_client.get("/v1/status", headers=AUTH)).json()
    assert off["code_access"] is False
    assert on["code_access"] is True
    assert off["sync_running"] is True  # the supervisor's snapshot is still there


@pytest.mark.parametrize("url", ["/v1/code/tree", "/v1/code/file"])
async def test_on_the_routes_still_need_the_token(code_client, url: str) -> None:
    resp = await code_client.post(url, json={"roots": []})
    assert resp.status == 401


# -- what is served -------------------------------------------------------


async def test_tree_lists_every_class_including_unclassified_and_personal(code_client, vault: Path, settings) -> None:
    write(vault, "Library/k.md", note())
    write(vault, "Life/p.md", note())
    write(vault, "Lens/l.md", note())
    write(vault, "Loose/u.md", note())
    write(vault, "Loose/marked.md", note("personal"))
    write(vault, "root.md", note())
    got = await tree(code_client)
    by_path = {f["path"]: f for f in got["files"]}
    assert [f["path"] for f in got["files"]] == sorted(by_path)
    assert {p: f["class"] for p, f in by_path.items()} == {
        "Library/k.md": "knowledge",
        "Life/p.md": "personal",
        "Lens/l.md": "lens",
        "Loose/u.md": "none",
        "Loose/marked.md": "personal",
        "root.md": "none",
    }
    assert got["truncated"] is False
    entry = by_path["Loose/u.md"]
    assert set(entry) == {"path", "class", "bytes", "mtime"}
    assert entry["bytes"] == len(note().encode())
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", entry["mtime"])


async def test_tree_mtime_is_utc_seconds(code_client, vault: Path) -> None:
    path = write(vault, "a.md", note())
    os.utime(path, (1_700_000_000, 1_700_000_000.75))
    assert (await tree(code_client))["files"][0]["mtime"] == "2023-11-14T22:13:20Z"


@pytest.mark.parametrize(
    "rel,mark",
    [("Loose/u.md", None), ("Life/p.md", None), ("Library/k.md", "knowledge"), ("Lens/l.md", None)],
)
async def test_file_serves_unclassified_personal_knowledge_and_lens(code_client, vault: Path, settings, rel, mark) -> None:
    text = note(mark, body="Текст заметки.\n")
    write(vault, rel, text)
    resp = await read(code_client, rel)
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == {"path", "class", "sha256", "text"}
    assert body["path"] == rel
    assert body["text"] == text
    assert body["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    expected = {"Loose": "none", "Life": "personal", "Library": "knowledge", "Lens": "lens"}[rel.split("/")[0]]
    assert body["class"] == expected


async def test_missing_settings_file_is_fine_and_a_never_property_still_closes(code_client, vault: Path) -> None:
    write(vault, "a.md", note())
    write(vault, "b.md", note("never"))
    assert await tree_paths(code_client) == ["a.md"]
    assert (await read(code_client, "a.md")).status == 200
    assert (await read(code_client, "b.md")).status == 404


# -- every file refusal is the same 404 -----------------------------------


def _symlink_file(vault: Path) -> str:
    write(vault, "Notes/real.md", note())
    os.symlink(vault / "Notes" / "real.md", vault / "Notes" / "link.md")
    return "Notes/link.md"


def _symlink_folder(vault: Path) -> str:
    write(vault, "Real/a.md", note())
    os.symlink(vault / "Real", vault / "Linked")
    return "Linked/a.md"


def _symlink_out(vault: Path) -> str:
    outside = vault.parent / "outside"
    outside.mkdir()
    (outside / "a.md").write_text(note())
    os.symlink(outside, vault / "Out")
    return "Out/a.md"


REFUSALS = {
    "missing": lambda v: "Notes/nothing.md",
    "folder": lambda v: (write(v, "Notes/Sub.md/x.md", note()), "Notes/Sub.md")[1],
    "symlinked file": _symlink_file,
    "symlinked folder": _symlink_folder,
    "symlink out of the vault": _symlink_out,
    "dot folder": lambda v: (write(v, ".obsidian/a.md", note()), ".obsidian/a.md")[1],
    "dot file": lambda v: (write(v, "Notes/.hidden.md", note()), "Notes/.hidden.md")[1],
    "anchor memory": lambda v: (write(v, "Anchor/Memory/0001-a.md", "fact\n"), "Anchor/Memory/0001-a.md")[1],
    "anchor lower case": lambda v: (write(v, "anchor/Memory/x.md", note()), "anchor/Memory/x.md")[1],
    "anchor upper case": lambda v: (write(v, "ANCHOR/x.md", note()), "ANCHOR/x.md")[1],
    "settings file": lambda v: "Anchor/settings.md",
    "never by property": lambda v: (write(v, "Library/n.md", note("never")), "Library/n.md")[1],
    "never by folder rule": lambda v: (write(v, "Life/Diary/d.md", note("knowledge")), "Life/Diary/d.md")[1],
    "never folder in another case": lambda v: (write(v, "vault/s.md", note()), "vault/s.md")[1],
    "not md": lambda v: (write(v, "Notes/a.txt", "plain\n"), "Notes/a.txt")[1],
    "not utf8": lambda v: (write(v, "Notes/bin.md", b"\xff\xfe bad"), "Notes/bin.md")[1],
    "too large": lambda v: (write(v, "Notes/big.md", "x" * (CODE_FILE_MAX_BYTES + 1)), "Notes/big.md")[1],
    "unreadable anchor value": lambda v: (write(v, "Notes/typo.md", note("nevr")), "Notes/typo.md")[1],
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
async def test_every_file_refusal_is_the_same_404(code_client, vault: Path, settings, case: str) -> None:
    rel = REFUSALS[case](vault)
    resp = await read(code_client, rel)
    assert resp.status == 404
    assert await resp.json() == NOT_FOUND
    assert rel not in await tree_paths(code_client)


async def test_tree_leaves_out_every_refused_file(code_client, vault: Path, settings) -> None:
    for make in REFUSALS.values():
        make(vault)
    write(vault, "Notes/ok.md", note())
    # `Notes/Sub.md` is a folder: refused as a file, walked as a folder.
    assert await tree_paths(code_client) == ["Notes/Sub.md/x.md", "Notes/ok.md", "Notes/real.md", "Real/a.md"]


async def test_a_file_that_is_exactly_the_cap_is_served(code_client, vault: Path) -> None:
    write(vault, "big.md", "x" * CODE_FILE_MAX_BYTES)
    assert (await read(code_client, "big.md")).status == 200


# -- roots ----------------------------------------------------------------


async def test_roots_confine_by_segment_not_by_prefix(code_client, vault: Path) -> None:
    write(vault, "Notes/a.md", note())
    write(vault, "Notes/Sub/b.md", note())
    write(vault, "Notes2/x.md", note())
    write(vault, "Notes.md", note())
    assert await tree_paths(code_client, ["Notes"]) == ["Notes/Sub/b.md", "Notes/a.md"]
    assert (await read(code_client, "Notes/Sub/b.md", ["Notes"])).status == 200
    for rel in ("Notes2/x.md", "Notes.md"):
        resp = await read(code_client, rel, ["Notes"])
        assert resp.status == 404
        assert await resp.json() == NOT_FOUND


async def test_several_roots_are_merged_and_overlaps_listed_once(code_client, vault: Path) -> None:
    write(vault, "A/a.md", note())
    write(vault, "A/Deep/d.md", note())
    write(vault, "B/b.md", note())
    write(vault, "C/c.md", note())
    assert await tree_paths(code_client, ["B", "A", "A/Deep", "A"]) == ["A/Deep/d.md", "A/a.md", "B/b.md"]
    assert (await read(code_client, "B/b.md", ["A", "B"])).status == 200
    assert (await read(code_client, "C/c.md", ["A", "B"])).status == 404


async def test_a_note_cannot_be_its_own_root(code_client, vault: Path) -> None:
    write(vault, "Notes/a.md", note())
    assert (await read(code_client, "Notes/a.md", ["Notes/a.md"])).status == 404


async def test_a_root_that_would_be_refused_contributes_nothing(code_client, vault: Path, settings) -> None:
    write(vault, "Notes/a.md", note())
    write(vault, "Anchor/Memory/0001-a.md", "fact\n")
    write(vault, "anchor/Memory/x.md", note())
    write(vault, ".obsidian/a.md", note())
    write(vault, "Life/Diary/d.md", note())
    _symlink_folder(vault)
    for root in ("Missing", "Notes/a.md", "Anchor", "Anchor/Memory", "anchor", "ANCHOR/Memory", ".obsidian", "Linked", "Life/Diary"):
        got = await tree(code_client, [root])
        assert got == {"files": [], "truncated": False}, root
    # A refused root next to a good one does not spoil it.
    assert await tree_paths(code_client, ["Missing", "Anchor", "Notes"]) == ["Notes/a.md"]


async def test_the_whole_vault_skips_anchor_but_not_a_nested_folder_named_anchor(code_client, vault: Path) -> None:
    write(vault, "Anchor/Journal/2026-11-16.md", "day\n")
    write(vault, "Projects/Anchor/plan.md", note())
    assert await tree_paths(code_client) == ["Projects/Anchor/plan.md"]
    assert (await read(code_client, "Projects/Anchor/plan.md")).status == 200


# -- bodies ---------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"roots": "Notes"},
        {"roots": [1]},
        {"roots": [], "extra": 1},
        {"roots": ["a"] * (CODE_ROOTS_MAX + 1)},
        [],
        "roots",
    ],
)
async def test_tree_bad_bodies_are_400(code_client, body) -> None:
    resp = await code_client.post("/v1/code/tree", json=body, headers=AUTH)
    assert resp.status == 400
    assert await resp.json() == {"error": "bad_body"}


async def test_tree_accepts_the_most_roots(code_client) -> None:
    assert (await tree(code_client, [f"R{i}" for i in range(CODE_ROOTS_MAX)]))["files"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"path": "a.md"},
        {"roots": []},
        {"path": 1, "roots": []},
        {"path": "a.md", "roots": None},
        {"path": "a.md", "roots": [], "extra": 1},
        {"path": "a.md", "roots": ["a"] * (CODE_ROOTS_MAX + 1)},
    ],
)
async def test_file_bad_bodies_are_400(code_client, body) -> None:
    resp = await code_client.post("/v1/code/file", json=body, headers=AUTH)
    assert resp.status == 400
    assert await resp.json() == {"error": "bad_body"}


@pytest.mark.parametrize("url", ["/v1/code/tree", "/v1/code/file"])
async def test_a_body_that_is_not_json_is_400(code_client, url: str) -> None:
    resp = await code_client.post(url, data=b"not json", headers=AUTH)
    assert resp.status == 400
    assert await resp.json() == {"error": "bad_body"}


@pytest.mark.parametrize("bad", ["", "../x.md", "/x.md", "a//b.md", "a/./b.md", "a\\b.md", "a/"])
async def test_malformed_paths_and_roots_are_400(code_client, bad: str) -> None:
    resp = await code_client.post("/v1/code/file", json={"path": bad, "roots": []}, headers=AUTH)
    assert resp.status == 400
    assert await resp.json() == {"error": "malformed_path"}
    resp = await code_client.post("/v1/code/file", json={"path": "a.md", "roots": [bad]}, headers=AUTH)
    assert await resp.json() == {"error": "malformed_path"}
    resp = await code_client.post("/v1/code/tree", json={"roots": [bad]}, headers=AUTH)
    assert resp.status == 400
    assert await resp.json() == {"error": "malformed_path"}


# -- the settings file fails closed ---------------------------------------


def _invalid(vault: Path) -> None:
    write(vault, "Anchor/settings.md", "---\nanchor: settings\nnever_folder: [Life/Diary]\n---\n")


def _wrong_case(vault: Path) -> None:
    write(vault, "Anchor/Settings.md", SETTINGS)


@pytest.mark.parametrize("make", [_invalid, _wrong_case], ids=["invalid", "wrong_case"])
async def test_an_unusable_settings_file_is_409_on_both_routes(code_client, vault: Path, make) -> None:
    write(vault, "Notes/a.md", note())
    make(vault)
    for url, body in (
        ("/v1/code/tree", {"roots": []}),
        ("/v1/code/file", {"path": "Notes/a.md", "roots": []}),
        ("/v1/code/file", {"path": "Notes/missing.md", "roots": []}),
    ):
        resp = await code_client.post(url, json=body, headers=AUTH)
        assert resp.status == 409
        assert await resp.json() == {"error": "settings_unusable"}


# -- the cap --------------------------------------------------------------


async def test_the_tree_cap_keeps_the_first_paths_and_says_truncated(code_client, vault: Path, monkeypatch) -> None:
    monkeypatch.setattr(code, "CODE_TREE_MAX", 3)
    for name in ("e", "a", "d", "b", "c"):
        write(vault, f"Notes/{name}.md", note())
    got = await tree(code_client)
    assert [f["path"] for f in got["files"]] == ["Notes/a.md", "Notes/b.md", "Notes/c.md"]
    assert got["truncated"] is True
    monkeypatch.setattr(code, "CODE_TREE_MAX", 5)
    assert (await tree(code_client))["truncated"] is False


def test_the_cap_is_five_thousand() -> None:
    assert code.CODE_TREE_MAX == 5000
    assert CODE_FILE_MAX_BYTES == 2 * 1024 * 1024
    assert CODE_ROOTS_MAX == 16


# -- logs -----------------------------------------------------------------

TITLE = "Секретный проект"
BODY = "мой текст о здоровье"
FOLDER = "Личное"


async def test_no_path_root_or_text_is_logged(code_client, vault: Path, settings, caplog) -> None:
    write(vault, f"{FOLDER}/{TITLE}.md", note(body=BODY))
    write(vault, f"Life/Diary/{TITLE}-never.md", note(body=BODY))
    with caplog.at_level(logging.DEBUG):
        await tree(code_client)
        await tree(code_client, [FOLDER])
        await tree(code_client, [f"{FOLDER}-missing"])
        assert (await read(code_client, f"{FOLDER}/{TITLE}.md", [FOLDER])).status == 200
        await read(code_client, f"Life/Diary/{TITLE}-never.md")
        await read(code_client, f"{FOLDER}/{TITLE}.md", ["Elsewhere"])
        await read(code_client, f"../{TITLE}.md")
        _invalid(vault)
        resp = await code_client.post("/v1/code/tree", json={"roots": [FOLDER]}, headers=AUTH)
        assert resp.status == 409
    logged = _everything_logged(caplog)
    for secret in (TITLE, BODY, FOLDER, "Diary", "Elsewhere"):
        assert secret not in logged
    requests = [r for r in caplog.records if r.getMessage() == "request"]
    assert {r.__dict__["route"] for r in requests} == {"/v1/code/tree", "/v1/code/file"}
    assert {r.__dict__["status"] for r in requests} == {200, 400, 404, 409}
    counts = [r.__dict__["count"] for r in caplog.records if r.getMessage() == "code_tree"]
    assert counts == [1, 1, 0]
