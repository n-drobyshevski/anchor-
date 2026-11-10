"""Echo's stream notes: `PUT /v1/echo/streams` (streams.py).

Pinned here:

- the note is `anchor: never`, with frontmatter vaultd wrote itself, in
  `<echo_inbox>/Streams`, and it is invisible on every read route, in
  the manifest and in the graph;
- vaultd builds the path from a bare basename; no inbox, no stream notes;
- an existing file is replaced only when it is this writer's own
  untouched note for the same stream: a body the user edited, a note
  whose properties changed, another stream's note and the user's own
  file are all left exactly as they are;
- an unchanged body writes nothing and spends no changeset;
- every write is an undoable Echo changeset, `purpose: streams`, with
  its own caps that neither spend nor are spent by the inbox writer's;
- the log names the refusal's reason and never the name, key or body.

Every note here is synthetic.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from vaultd import frontmatter, streams
from vaultd.config import (
    ECHO_CHANGESETS_PER_DAY,
    ECHO_WRITE_MAX_BYTES,
    STREAM_CHANGESETS_PER_DAY,
    STREAM_FILES_PER_CHANGESET,
)
from vaultd.log import JsonFormatter
from tests.conftest import AUTH, write

SETTINGS = "---\nanchor: settings\nknowledge_folders: [Library]\n---\n"
NAME = "диплом.md"
STREAM = "диплом"
BODY = "# диплом\n\nОткрыто: 3. Требует внимания: 1.\n\n- Глава 2 (завтра)\n"
REL = f"Echo/Inbox/Streams/{NAME}"


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


async def _put(client, name: str = NAME, stream: str = STREAM, body: str = BODY, changeset: str = "streams_a"):
    return await client.put(
        "/v1/echo/streams",
        json={"name": name, "stream": stream, "body": body, "changeset": changeset},
        headers=AUTH,
    )


async def _put_inbox(client, name: str, changeset: str):
    content = '---\nanchor: knowledge\nsource_urls: ["https://example.org/x"]\ngap: 1\n---\nТекст.\n'
    return await client.put(
        "/v1/echo/inbox", json={"name": name, "content": content, "changeset": changeset}, headers=AUTH
    )


async def _undo(client, changeset: str):
    return await client.post("/v1/undo", params={"changeset": changeset, "writer": "echo"}, headers=AUTH)


async def _changes(client) -> list[dict]:
    return (await (await client.get("/v1/changes", headers=AUTH)).json())["changes"]


@pytest.fixture
def base(vault: Path):
    write(vault, "Anchor/settings.md", SETTINGS)
    (vault / "Library").mkdir()


# -- create ----------------------------------------------------------------------


async def test_create_writes_a_never_note_with_vaultds_own_frontmatter(client, vault: Path, base):
    resp = await _put(client)
    assert resp.status == 200
    data = (vault / REL).read_bytes()
    assert await resp.json() == {"name": NAME, "sha256": sha(data), "changed": True}
    loaded = frontmatter.load(data)
    assert set(loaded) == streams.OWN_KEYS
    assert loaded["anchor"] == "never"
    assert loaded["echo_stream"] == STREAM
    assert loaded["anchor_edited_by"] == "echo"
    assert loaded["echo_body_sha256"] == sha(BODY)
    assert frontmatter.body(data) == BODY.encode()
    assert frontmatter.note_mark(data) == "never"
    changes = await _changes(client)
    assert [(c["kind"], c["writer"], c.get("purpose")) for c in changes] == [("write", "echo", "streams")]


async def test_the_note_is_invisible_to_every_read_route(client, vault: Path, base):
    assert (await _put(client)).status == 200
    for route in ("/v1/file", "/v1/knowledge"):
        assert (await client.get(route, params={"path": REL}, headers=AUTH)).status == 404
    listings = []
    for route in ("/v1/manifest", "/v1/knowledge/tree", "/v1/knowledge/graph"):
        resp = await client.get(route, headers=AUTH)
        assert resp.status == 200
        listings.append(await resp.text())
    for listing in listings:
        assert "диплом" not in listing
        assert "\\u0434\\u0438\\u043f" not in listing  # the same name, JSON-escaped
    # The folder itself sits inside the inbox, a knowledge folder, so the
    # tree names it (a constant, not the user's data) -- and nothing in it.
    assert (await (await client.get("/v1/knowledge/tree", headers=AUTH)).json())["notes"] == []


async def test_a_body_that_looks_like_frontmatter_stays_body(client, vault: Path, base):
    body = "---\nanchor: lens\n---\nтекст\n"
    assert (await _put(client, body=body)).status == 200
    data = (vault / REL).read_bytes()
    assert frontmatter.load(data)["anchor"] == "never"
    assert frontmatter.body(data) == body.encode()


async def test_an_all_digit_looking_hash_still_round_trips():
    """The hash is quoted, so YAML can never read it as a number."""
    data = streams.compose(STREAM, BODY.encode(), "2026-11-10T00:00:00+00:00")
    assert b'echo_body_sha256: "' in data
    assert streams.own_body(data, STREAM) == BODY.encode()


@pytest.mark.parametrize(
    "name",
    ["sub/x.md", "../x.md", ".hidden.md", "x.txt", ".md", "x\n.md", "a" * 121 + ".md", "Echo/Inbox/Streams/x.md"],
)
async def test_a_name_that_is_not_a_bare_safe_basename_is_refused(client, vault: Path, base, name):
    assert (await _put(client, name=name)).status == 403
    assert not (vault / "Echo").exists()


@pytest.mark.parametrize("stream", ["", "a" * 121, "two\nlines", "zero​width", "é"])
async def test_a_bad_stream_key_is_refused(client, vault: Path, base, stream):
    assert (await _put(client, stream=stream)).status == 403
    assert not (vault / "Echo").exists()


async def test_a_body_over_the_cap_is_refused(client, vault: Path, base):
    assert (await _put(client, body="a" * (ECHO_WRITE_MAX_BYTES + 1))).status == 403
    assert not (vault / "Echo").exists()


async def test_no_settings_file_or_an_invalid_one_means_no_stream_notes(client, vault: Path):
    assert (await _put(client)).status == 403
    write(vault, "Anchor/settings.md", "---\nanchor: settings\nnever_folder: [Diary]\n---\n")
    assert (await _put(client)).status == 403
    assert not (vault / "Echo").exists()


async def test_an_explicit_inbox_moves_the_folder(client, vault: Path):
    write(vault, "Anchor/settings.md", "---\nanchor: settings\necho_inbox: Входящие\n---\n")
    assert (await _put(client)).status == 200
    assert (vault / "Входящие" / "Streams" / NAME).exists()


async def test_a_symlinked_folder_is_refused(client, vault: Path, base, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "Echo" / "Inbox").mkdir(parents=True)
    (vault / "Echo" / "Inbox" / "Streams").symlink_to(outside)
    assert (await _put(client)).status == 403
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"name": NAME, "stream": STREAM, "body": BODY},
        {"name": NAME, "stream": STREAM, "body": BODY, "changeset": "x", "path": "a"},
        {"name": NAME, "stream": STREAM, "body": 1, "changeset": "streams_a"},
        {"name": NAME, "stream": STREAM, "body": BODY, "changeset": "../x"},
    ],
)
async def test_a_malformed_body_is_a_400(client, base, payload):
    assert (await client.put("/v1/echo/streams", json=payload, headers=AUTH)).status == 400


async def test_the_route_needs_the_token(client):
    resp = await client.put(
        "/v1/echo/streams", json={"name": NAME, "stream": STREAM, "body": BODY, "changeset": "streams_a"}
    )
    assert resp.status == 401


# -- replace ---------------------------------------------------------------------


async def test_an_untouched_note_is_replaced_and_the_same_body_writes_nothing(client, vault: Path, base):
    assert (await _put(client)).status == 200
    first = (vault / REL).read_bytes()
    again = await _put(client, changeset="streams_b")
    assert await again.json() == {"name": NAME, "sha256": sha(first), "changed": False}
    assert (vault / REL).read_bytes() == first
    assert len(await _changes(client)) == 1

    resp = await _put(client, body=BODY + "- ещё\n", changeset="streams_b")
    assert (await resp.json())["changed"] is True
    data = (vault / REL).read_bytes()
    assert frontmatter.body(data) == (BODY + "- ещё\n").encode()
    assert len(await _changes(client)) == 2


async def test_a_body_the_user_edited_is_left_alone(client, vault: Path, base):
    assert (await _put(client)).status == 200
    path = vault / REL
    edited = path.read_text() + "моя заметка на полях\n"
    path.write_text(edited)
    assert (await _put(client, body="новое\n", changeset="streams_b")).status == 403
    assert path.read_text() == edited
    # ... even when the bot sends what the user's edit happens to equal.
    assert (await _put(client, body=BODY + "моя заметка на полях\n", changeset="streams_c")).status == 403


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda text: text.replace("anchor: never", "anchor: knowledge"),
        lambda text: text.replace("anchor_edited_by: echo", "anchor_edited_by: claude"),
        lambda text: text.replace("---\nanchor: never", "---\ntags: [x]\nanchor: never"),
        lambda text: text.split("---\n", 2)[2],
    ],
)
async def test_a_note_whose_properties_changed_is_left_alone(client, vault: Path, base, rewrite):
    assert (await _put(client)).status == 200
    path = vault / REL
    changed = rewrite(path.read_text())
    path.write_text(changed)
    assert (await _put(client, body="новое\n", changeset="streams_b")).status == 403
    assert path.read_text() == changed


async def test_another_streams_note_and_the_users_own_file_are_never_replaced(client, vault: Path, base):
    assert (await _put(client)).status == 200
    assert (await _put(client, stream="работа", changeset="streams_b")).status == 403
    assert frontmatter.load((vault / REL).read_bytes())["echo_stream"] == STREAM

    write(vault, "Echo/Inbox/Streams/моё.md", "мой файл\n")
    assert (await _put(client, name="моё.md", changeset="streams_c")).status == 403
    assert (vault / "Echo/Inbox/Streams/моё.md").read_text() == "мой файл\n"


# -- undo and caps -----------------------------------------------------------------


async def test_undo_removes_a_new_note_and_restores_a_replaced_one(client, vault: Path, base):
    assert (await _put(client)).status == 200
    first = (vault / REL).read_bytes()
    assert (await _put(client, body="второе\n", changeset="streams_b")).status == 200
    assert await (await _undo(client, "streams_b")).json() == {"restored": 1, "refused": 0}
    assert (vault / REL).read_bytes() == first
    # The restored note is still Echo's own, so a refresh can replace it again.
    assert (await _put(client, body="третье\n", changeset="streams_c")).status == 200

    assert (await _undo(client, "streams_c")).status == 200
    assert await (await _undo(client, "streams_a")).json() == {"restored": 1, "refused": 0}
    assert not (vault / "Echo").exists()


async def test_one_refresh_is_one_changeset_up_to_its_file_cap(client, vault: Path, base):
    for i in range(STREAM_FILES_PER_CHANGESET):
        assert (await _put(client, name=f"s{i}.md", stream=f"s{i}")).status == 200
    assert (await _put(client, name="late.md", stream="late")).status == 403
    assert not (vault / "Echo/Inbox/Streams/late.md").exists()
    changes = await _changes(client)
    assert len(changes) == 1 and len(changes[0]["files"]) == STREAM_FILES_PER_CHANGESET


async def test_the_daily_cap_is_the_streams_own(client, vault: Path, base, clock):
    for i in range(STREAM_CHANGESETS_PER_DAY):
        assert (await _put(client, body=f"{i}\n", changeset=f"streams_{i}")).status == 200
    assert (await _put(client, body="late\n", changeset="streams_late")).status == 403
    # The inbox writer still has its whole budget ...
    for i in range(ECHO_CHANGESETS_PER_DAY):
        assert (await _put_inbox(client, f"n{i}.md", f"echo_{i}")).status == 200
    assert (await _put_inbox(client, "late.md", "echo_late")).status == 403
    # ... and a full inbox budget does not block the next day's refresh.
    clock.advance(hours=24, seconds=1)
    assert (await _put(client, body="late\n", changeset="streams_late")).status == 200


async def test_the_two_echo_writers_cannot_share_a_changeset(client, vault: Path, base):
    assert (await _put(client)).status == 200
    assert (await _put_inbox(client, "n.md", "streams_a")).status == 403
    assert (await _put_inbox(client, "n.md", "echo_a")).status == 200
    assert (await _put(client, name="x.md", stream="x", changeset="echo_a")).status == 403
    assert not (vault / "Echo/Inbox/Streams/x.md").exists()


async def test_a_claude_changeset_cannot_be_joined(client, vault: Path, base):
    resp = await client.put(
        "/v1/knowledge",
        params={"path": "Library/a.md"},
        json={"content": "a\n", "if_sha256": None, "changeset": "shared"},
        headers=AUTH,
    )
    assert resp.status == 200
    assert (await _put(client, changeset="shared")).status == 403
    assert not (vault / "Echo").exists()

    assert (await _put(client, changeset="streams_b")).status == 200
    resp = await client.put(
        "/v1/knowledge",
        params={"path": "Library/b.md"},
        json={"content": "b\n", "if_sha256": None, "changeset": "streams_b"},
        headers=AUTH,
    )
    assert resp.status == 403
    assert not (vault / "Library" / "b.md").exists()


async def test_logs_carry_the_reason_and_never_the_name_key_or_body(client, vault: Path, base, caplog):
    with caplog.at_level(logging.DEBUG):
        assert (await _put(client)).status == 200
        path = vault / REL
        path.write_text(path.read_text() + "правка\n")
        assert (await _put(client, body="секретная задача\n", changeset="streams_b")).status == 403
    formatter = JsonFormatter()
    dumped = "\n".join(
        part for r in caplog.records for part in (r.getMessage(), str(r.__dict__), formatter.format(r))
    )
    assert "stream_edited" in dumped
    for leak in ("диплом", "секретная", "Streams", "Echo/Inbox", "Глава"):
        assert leak not in dumped


def test_the_stream_caps_are_small_constants():
    assert (STREAM_FILES_PER_CHANGESET, STREAM_CHANGESETS_PER_DAY) == (12, 6)
