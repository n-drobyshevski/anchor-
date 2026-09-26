"""The undo store: caps, TTL and compare-and-swap in the undo direction (write-plan section 6.2, 6.4)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from vaultd.boot import BootRefused, check_undo_root
from vaultd.config import Config
from vaultd.undo import CapExceeded, FileEntry, UndoStore
from tests.conftest import AUTH, FakeClock, write

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


# -- restores byte for byte, refused after a later edit ----------------------


async def test_undo_restores_an_update_byte_for_byte(client, vault: Path):
    write(vault, "Library/CCRU.md", note())
    resp = await _put(client, "Library/CCRU.md", note(body="New.\n"), sha(note()), changeset="cs")
    assert resp.status == 200

    undo = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert undo.status == 200
    assert (await undo.json()) == {"restored": 1, "refused": 0}
    assert (vault / "Library" / "CCRU.md").read_bytes() == note().encode()


async def test_a_created_file_is_deleted_only_if_unchanged(client, vault: Path):
    resp = await _put(client, "Library/CCRU.md", "Hello.\n", None, changeset="cs")
    assert resp.status == 200

    write(vault, "Library/CCRU.md", "edited on the phone\n")
    undo = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert undo.status == 200
    assert (await undo.json()) == {"restored": 0, "refused": 1}
    assert (vault / "Library" / "CCRU.md").read_text() == "edited on the phone\n"


async def test_a_created_file_is_deleted_when_unchanged(client, vault: Path):
    resp = await _put(client, "Library/CCRU.md", "Hello.\n", None, changeset="cs")
    assert resp.status == 200

    undo = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert (await undo.json()) == {"restored": 1, "refused": 0}
    assert not (vault / "Library" / "CCRU.md").exists()


async def test_refused_per_file_after_a_later_edit(client, vault: Path):
    write(vault, "Library/CCRU.md", note())
    resp = await _put(client, "Library/CCRU.md", note(body="New.\n"), sha(note()), changeset="cs")
    assert resp.status == 200

    write(vault, "Library/CCRU.md", "edited on the phone\n")
    undo = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert (await undo.json()) == {"restored": 0, "refused": 1}
    assert (vault / "Library" / "CCRU.md").read_text() == "edited on the phone\n"


# -- no route accepts content for undo ---------------------------------------


async def test_undo_takes_no_body(client, vault: Path):
    write(vault, "Library/CCRU.md", note())
    resp = await _put(client, "Library/CCRU.md", note(body="New.\n"), sha(note()), changeset="cs")
    assert resp.status == 200
    # A JSON body on the undo request is simply never read.
    undo = await client.post("/v1/undo", params={"changeset": "cs"}, json={"content": "malicious"}, headers=AUTH)
    assert undo.status == 200
    assert (vault / "Library" / "CCRU.md").read_bytes() == note().encode()


# -- an undo of an undo is refused --------------------------------------------


async def test_undo_of_an_undo_is_refused(client, vault: Path):
    write(vault, "Library/CCRU.md", note())
    resp = await _put(client, "Library/CCRU.md", note(body="New.\n"), sha(note()), changeset="cs")
    assert resp.status == 200
    first = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert first.status == 200

    changes = await (await client.get("/v1/changes", headers=AUTH)).json()
    undo_ids = [c["id"] for c in changes["changes"] if c["kind"] == "undo"]
    assert len(undo_ids) == 1

    second = await client.post("/v1/undo", params={"changeset": undo_ids[0]}, headers=AUTH)
    assert second.status == 403
    assert await second.read() == b""


async def test_undo_of_a_missing_changeset_is_404(client) -> None:
    resp = await client.post("/v1/undo", params={"changeset": "nope"}, headers=AUTH)
    assert resp.status == 404


# -- caps ----------------------------------------------------------------------


async def test_files_per_changeset_cap(client, vault: Path):
    for i in range(5):
        resp = await _put(client, f"Library/F{i}.md", "x", None, changeset="big")
        assert resp.status == 200
    resp = await _put(client, "Library/F5.md", "x", None, changeset="big")
    assert resp.status == 403
    assert not (vault / "Library" / "F5.md").exists()


async def test_changesets_per_hour_cap(client, vault: Path):
    for i in range(4):
        resp = await _put(client, f"Library/F{i}.md", "x", None, changeset=f"cs{i}")
        assert resp.status == 200
    resp = await _put(client, "Library/F4.md", "x", None, changeset="cs4")
    assert resp.status == 403
    assert not (vault / "Library" / "F4.md").exists()


async def test_changesets_per_hour_cap_clears_after_an_hour(client, vault: Path, clock: FakeClock):
    for i in range(4):
        resp = await _put(client, f"Library/F{i}.md", "x", None, changeset=f"cs{i}")
        assert resp.status == 200
    clock.advance(hours=1, minutes=1)
    resp = await _put(client, "Library/F4.md", "x", None, changeset="cs4")
    assert resp.status == 200


async def test_undos_per_hour_cap(client, vault: Path, clock: FakeClock):
    changesets = []
    after_write = None
    for i in range(5):
        write(vault, f"Library/F{i}.md", note())
        put = await _put(client, f"Library/F{i}.md", note(body="new\n"), sha(note()), changeset=f"cs{i}")
        assert put.status == 200
        changesets.append(f"cs{i}")
        if i == 4:
            after_write = (vault / "Library" / "F4.md").read_bytes()
        # Each write is its own changeset; space them out so the
        # changesets/hour cap (also 4) never trips here -- this test
        # is only about undos/hour.
        clock.advance(hours=1, minutes=1)

    for cs in changesets[:4]:
        undo = await client.post("/v1/undo", params={"changeset": cs}, headers=AUTH)
        assert undo.status == 200

    undo5 = await client.post("/v1/undo", params={"changeset": changesets[4]}, headers=AUTH)
    assert undo5.status == 403
    assert (vault / "Library" / "F4.md").read_bytes() == after_write


# -- TTL, with a frozen clock --------------------------------------------------


async def test_ttl_expiry_makes_a_changeset_unundoable(client, vault: Path, clock: FakeClock):
    write(vault, "Library/CCRU.md", note())
    resp = await _put(client, "Library/CCRU.md", note(body="New.\n"), sha(note()), changeset="cs")
    assert resp.status == 200

    clock.advance(days=14, minutes=1)
    undo = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert undo.status == 404

    changes = await (await client.get("/v1/changes", headers=AUTH)).json()
    assert changes["changes"] == []


async def test_ttl_does_not_expire_early(client, vault: Path, clock: FakeClock):
    write(vault, "Library/CCRU.md", note())
    resp = await _put(client, "Library/CCRU.md", note(body="New.\n"), sha(note()), changeset="cs")
    assert resp.status == 200

    clock.advance(days=13, hours=23)
    undo = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert undo.status == 200


# -- purge wipes the store ------------------------------------------------------


async def test_purge_wipes_the_undo_store(client, vault: Path):
    write(vault, "Library/CCRU.md", note())
    resp = await _put(client, "Library/CCRU.md", note(body="New.\n"), sha(note()), changeset="cs")
    assert resp.status == 200

    await client.post("/v1/purge", headers=AUTH)
    changes = await (await client.get("/v1/changes", headers=AUTH)).json()
    assert changes["changes"] == []
    undo = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert undo.status == 404


# -- the undo store itself, without the HTTP layer ------------------------------


def test_precheck_raises_without_writing_anything(tmp_path: Path):
    store = UndoStore(tmp_path / "undo")
    store.precheck("cs", "write", 5)
    with pytest.raises(CapExceeded):
        store.precheck("cs2", "write", 999)
    assert store.list_changes() == []


def test_append_rejects_a_changeset_id_reused_for_a_different_kind(tmp_path: Path):
    store = UndoStore(tmp_path / "undo")
    store.append("cs", "write", [FileEntry("a.md", None, "0" * 64)])
    with pytest.raises(CapExceeded):
        store.precheck("cs", "undo", 1)


# -- the undo root must live outside the vault ----------------------------------


def test_undo_root_inside_the_vault_is_refused_at_startup(tmp_path: Path):
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    cfg = Config(
        api_token="x" * 32,
        auth_token="a",
        vault="v",
        e2ee_password="p",
        device_name="d",
        vault_path=vault_path,
        config_home=tmp_path / "config",
        port=8080,
        undo_root=vault_path / "anchor-undo",
    )
    with pytest.raises(BootRefused):
        check_undo_root(cfg)


def test_undo_root_outside_the_vault_is_accepted(tmp_path: Path):
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    cfg = Config(
        api_token="x" * 32,
        auth_token="a",
        vault="v",
        e2ee_password="p",
        device_name="d",
        vault_path=vault_path,
        config_home=tmp_path / "config",
        port=8080,
        undo_root=tmp_path / "anchor-undo",
    )
    check_undo_root(cfg)  # does not raise
