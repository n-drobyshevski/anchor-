"""User-set write caps: the store, `/v1/limits`, and the prechecks honouring them."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from vaultd import limits
from vaultd.config import CHANGESETS_PER_HOUR, FILES_PER_CHANGESET
from vaultd.undo import UndoStore
from tests.conftest import AUTH, write

SETTINGS = "---\nanchor: settings\nknowledge_folders: [Library]\n---\n"


@pytest.fixture(autouse=True)
def _base_settings(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    (vault / "Library").mkdir(exist_ok=True)


async def _put(client, path: str, changeset: str):
    return await client.put(
        "/v1/knowledge",
        params={"path": path},
        json={"content": "x", "if_sha256": None, "changeset": changeset},
        headers=AUTH,
    )


# -- the store -----------------------------------------------------------------


def test_missing_file_reads_as_the_defaults(tmp_path: Path):
    assert limits.LimitsStore(tmp_path / "limits.json").get() == limits.DEFAULTS


def test_defaults_are_the_config_constants():
    assert limits.DEFAULTS.files_per_changeset == FILES_PER_CHANGESET
    assert limits.DEFAULTS.changesets_per_hour == CHANGESETS_PER_HOUR


def test_round_trip(tmp_path: Path):
    store = limits.LimitsStore(tmp_path / "limits.json")
    new = limits.validate({"moves_per_day": 5})
    store.put(new)
    assert store.get() == new
    assert store.get().files_per_changeset == FILES_PER_CHANGESET


def test_corrupt_file_reads_as_the_defaults(tmp_path: Path):
    path = tmp_path / "limits.json"
    path.write_text("{not json")
    assert limits.LimitsStore(path).get() == limits.DEFAULTS


def test_bad_keys_in_the_file_fall_back_per_key(tmp_path: Path):
    path = tmp_path / "limits.json"
    path.write_text(json.dumps({"moves_per_day": 7, "files_per_changeset": 10_000, "evil": 1}))
    got = limits.LimitsStore(path).get()
    assert got.moves_per_day == 7
    assert got.files_per_changeset == FILES_PER_CHANGESET


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"nope": 1},
        {"files_per_changeset": 0},
        {"files_per_changeset": 201},
        {"moves_per_day": "5"},
        {"moves_per_day": True},
        {"moves_per_day": 1.5},
        {"counters_reset_at": -1},
        {"counters_reset_at": "2026-09-29"},
    ],
)
def test_validate_refuses(body):
    with pytest.raises(limits.Invalid):
        limits.validate(body)


def test_purge_resets_the_limits(tmp_path: Path):
    store = UndoStore(tmp_path / "undo")
    store.limits.put(limits.validate({"moves_per_day": 1}))
    store.purge()
    assert store.limits.get() == limits.DEFAULTS


# -- the routes ------------------------------------------------------------------


async def test_limits_routes_need_the_token(client):
    assert (await client.get("/v1/limits")).status == 401
    assert (await client.put("/v1/limits", json={})).status == 401


async def test_get_limits_reports_values_defaults_and_bounds(client):
    resp = await client.get("/v1/limits", headers=AUTH)
    assert resp.status == 200
    body = await resp.json()
    assert body["values"] == limits.DEFAULTS.as_json()
    assert body["defaults"] == limits.DEFAULTS.as_json()
    assert body["bounds"]["files_per_changeset"] == [1, 200]


async def test_put_limits_replaces_over_the_defaults(client):
    resp = await client.put("/v1/limits", json={"moves_per_day": 5, "undos_per_hour": 1}, headers=AUTH)
    assert resp.status == 200
    assert (await resp.json())["values"]["moves_per_day"] == 5
    # A second put without undos_per_hour brings it back to the default.
    resp = await client.put("/v1/limits", json={"moves_per_day": 6}, headers=AUTH)
    values = (await resp.json())["values"]
    assert values["moves_per_day"] == 6
    assert values["undos_per_hour"] == limits.DEFAULTS.undos_per_hour


@pytest.mark.parametrize("body", [{"nope": 1}, {"files_per_changeset": 999}, [1]])
async def test_put_limits_refuses_bad_bodies(client, body):
    resp = await client.put("/v1/limits", json=body, headers=AUTH)
    assert resp.status == 400
    assert (await (await client.get("/v1/limits", headers=AUTH)).json())["values"] == limits.DEFAULTS.as_json()


# -- the prechecks honour the stored values ----------------------------------------


async def test_a_lowered_files_cap_refuses_sooner(client, vault: Path):
    await client.put("/v1/limits", json={"files_per_changeset": 2}, headers=AUTH)
    assert (await _put(client, "Library/A.md", "cs")).status == 200
    assert (await _put(client, "Library/B.md", "cs")).status == 200
    assert (await _put(client, "Library/C.md", "cs")).status == 403
    assert not (vault / "Library" / "C.md").exists()


async def test_a_raised_changesets_cap_allows_more(client):
    await client.put("/v1/limits", json={"changesets_per_hour": CHANGESETS_PER_HOUR + 2}, headers=AUTH)
    for i in range(CHANGESETS_PER_HOUR + 2):
        assert (await _put(client, f"Library/F{i}.md", f"cs{i}")).status == 200
    assert (await _put(client, "Library/over.md", "over")).status == 403


async def test_zero_undos_per_hour_refuses_every_undo(client):
    assert (await _put(client, "Library/A.md", "cs")).status == 200
    await client.put("/v1/limits", json={"undos_per_hour": 0}, headers=AUTH)
    resp = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert resp.status == 403


# -- the counter reset -------------------------------------------------------------


async def test_a_counter_reset_gives_a_fresh_changeset_budget(client):
    for i in range(CHANGESETS_PER_HOUR):
        assert (await _put(client, f"Library/F{i}.md", f"cs{i}")).status == 200
    assert (await _put(client, "Library/over.md", "over")).status == 403
    # One second ahead, so the changesets above (same second) never count.
    reset_at = int(time.time()) + 1
    resp = await client.put("/v1/limits", json={"counters_reset_at": reset_at}, headers=AUTH)
    assert (await resp.json())["values"]["counters_reset_at"] == reset_at
    assert (await _put(client, "Library/after.md", "after")).status == 200


async def test_a_counter_reset_keeps_the_changesets_for_undo(client, vault: Path):
    assert (await _put(client, "Library/A.md", "cs")).status == 200
    await client.put("/v1/limits", json={"counters_reset_at": int(time.time()) + 1}, headers=AUTH)
    resp = await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
    assert resp.status == 200
    assert not (vault / "Library" / "A.md").exists()
