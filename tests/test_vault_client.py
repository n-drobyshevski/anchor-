"""app/vault/client.py against a loopback stub (phase-8 plan section 5.4).

No network beyond 127.0.0.1, no vaultd, no Obsidian.
"""

from __future__ import annotations

import datetime
import socket

import pytest

from app.vault import client as client_module
from app.vault import errors
from app.vault.client import NotesSummary, VaultClient
from app.vault.errors import VaultError
from vault_stub import TOKEN, start_stub


@pytest.fixture
async def stub():
    stub, server = await start_stub()
    yield stub
    await server.close()


def _client(stub) -> VaultClient:
    return VaultClient(stub.url, TOKEN)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_status_sends_the_bearer_token_and_parses(stub) -> None:
    stub.set_status(running=True, restarts=2)
    status = await _client(stub).status()
    assert status.sync_running is True
    assert status.restarts == 2
    assert status.running_since == datetime.datetime(2026, 9, 25, 8, 0, tzinfo=datetime.timezone.utc)
    assert stub.requests[0].authorization == f"Bearer {TOKEN}"
    assert stub.calls() == [("GET", "/v1/status")]


@pytest.mark.parametrize(
    "status,code",
    [
        (401, errors.UNAUTHORIZED),
        (404, errors.NOT_FOUND),
        (412, errors.CONFLICT),
        (400, errors.REFUSED),
        (403, errors.REFUSED),
        (413, errors.REFUSED),
        (422, errors.REFUSED),
        (500, errors.UNAVAILABLE),
        (503, errors.UNAVAILABLE),
        (418, errors.BAD_RESPONSE),
    ],
)
async def test_http_statuses_map_to_codes(stub, status, code) -> None:
    stub.respond("GET", "/v1/status", status, {"error": "whatever the server says"})
    with pytest.raises(VaultError) as exc:
        await _client(stub).status()
    assert exc.value.code == code


async def test_redirects_are_not_followed(stub) -> None:
    stub.respond("GET", "/v1/status", 302, "/elsewhere")
    with pytest.raises(VaultError) as exc:
        await _client(stub).status()
    assert exc.value.code == errors.BAD_RESPONSE
    assert stub.calls() == [("GET", "/v1/status")]


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        {"sync_running": "yes", "restarts": 0, "last_exit_code": None, "running_since": None},
        {"sync_running": True, "restarts": -1, "last_exit_code": None, "running_since": None},
        {"sync_running": True, "restarts": 0, "last_exit_code": None, "running_since": "yesterday"},
        {"sync_running": True, "restarts": 0, "last_exit_code": None, "running_since": "2026-09-25T08:00:00"},
    ],
)
async def test_malformed_bodies_are_bad_responses(stub, body) -> None:
    stub.respond("GET", "/v1/status", 200, body)
    with pytest.raises(VaultError) as exc:
        await _client(stub).status()
    assert exc.value.code == errors.BAD_RESPONSE


async def test_an_oversized_response_is_refused(stub, monkeypatch) -> None:
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(VaultError) as exc:
        await _client(stub).status()
    assert exc.value.code == errors.BAD_RESPONSE


async def test_connection_refused_is_unavailable() -> None:
    with pytest.raises(VaultError) as exc:
        await VaultClient(f"http://127.0.0.1:{_closed_port()}", TOKEN).status()
    assert exc.value.code == errors.UNAVAILABLE


async def test_a_hung_service_times_out_as_unavailable(stub, monkeypatch) -> None:
    monkeypatch.setattr(client_module, "TIMEOUT_S", 0.2)
    stub.delay_s = 2.0
    with pytest.raises(VaultError) as exc:
        await _client(stub).status()
    assert exc.value.code == errors.UNAVAILABLE


async def test_the_other_routes(stub) -> None:
    sha = "a" * 64
    stub.respond(
        "GET", "/v1/manifest", 200,
        {
            "files": [{"path": "Бег.md", "sha256": sha, "size": 10, "scope": "note", "class": "personal"}],
            "summary": {
                "conflict": 1,
                "legacy_read": 2,
                "unknown_value": 3,
                "settings": "valid",
                "knowledge_roots": ["Library"],
            },
        },
    )
    stub.respond(
        "GET", "/v1/file", 200, {"path": "Бег.md", "sha256": sha, "content": "текст", "class": "personal"}
    )
    stub.respond("PUT", "/v1/file", 200, {"sha256": sha})
    stub.respond("DELETE", "/v1/file", 200, {"deleted": True})
    stub.respond("POST", "/v1/purge", 200, {"deleted": 3})
    client = _client(stub)

    manifest = await client.manifest()
    [entry] = manifest.entries
    assert (entry.path, entry.scope, entry.note_class) == ("Бег.md", "note", "personal")
    assert manifest.summary == NotesSummary(
        conflict=1, legacy_read=2, unknown_value=3, settings="valid", knowledge_roots=("Library",)
    )
    got = await client.get_file("Бег.md")
    assert (got.content, got.note_class) == ("текст", "personal")
    assert await client.put_file("Anchor/Memory/0001-abcdef.md", "факт", None) == sha
    await client.delete_file("Anchor/Memory/0001-abcdef.md", sha)
    assert await client.purge() == 3

    assert stub.calls() == [
        ("GET", "/v1/manifest"),
        ("GET", "/v1/file"),
        ("PUT", "/v1/file"),
        ("DELETE", "/v1/file"),
        ("POST", "/v1/purge"),
    ]
    assert stub.requests[1].query == {"path": "Бег.md"}
    assert stub.requests[3].query == {"path": "Anchor/Memory/0001-abcdef.md", "if_sha256": sha}
    assert all(r.authorization == f"Bearer {TOKEN}" for r in stub.requests)


async def test_a_file_answer_for_another_path_is_refused(stub) -> None:
    stub.respond("GET", "/v1/file", 200, {"path": "other.md", "sha256": "a" * 64, "content": "x"})
    with pytest.raises(VaultError) as exc:
        await _client(stub).get_file("Бег.md")
    assert exc.value.code == errors.BAD_RESPONSE


def test_an_unknown_code_cannot_be_raised() -> None:
    with pytest.raises(ValueError):
        VaultError("some free text from a server")


SUMMARY = {"conflict": 0, "legacy_read": 0, "unknown_value": 0, "settings": "missing"}
NOTE = {"path": "Бег.md", "sha256": "a" * 64, "size": 10, "scope": "note"}
FACT = {"path": "Anchor/Memory/0001-abcdef.md", "sha256": "a" * 64, "size": 10, "scope": "anchor"}


@pytest.mark.parametrize(
    "body",
    [
        # 8e: a note entry with a missing or foreign class.
        {"files": [NOTE], "summary": SUMMARY},
        {"files": [{**NOTE, "class": "never"}], "summary": SUMMARY},
        {"files": [{**NOTE, "class": "Personal"}], "summary": SUMMARY},
        {"files": [{**NOTE, "class": ["personal"]}], "summary": SUMMARY},
        {"files": [{**NOTE, "class": None}], "summary": SUMMARY},
        # An Anchor file must carry no class at all.
        {"files": [{**FACT, "class": "personal"}], "summary": SUMMARY},
        {"files": [{**FACT, "class": None}], "summary": SUMMARY},
        # The summary: missing, or anything but counts and a known state.
        {"files": []},
        {"files": [], "summary": {**SUMMARY, "settings": "broken"}},
        {"files": [], "summary": {**SUMMARY, "conflict": -1}},
        {"files": [], "summary": {**SUMMARY, "conflict": True}},
        {"files": [], "summary": {**SUMMARY, "unknown_value": "3"}},
        {"files": [], "summary": {k: v for k, v in SUMMARY.items() if k != "legacy_read"}},
        # 8f: an old vaultd's "ok"/"absent" vocabulary is no longer known.
        {"files": [], "summary": {**SUMMARY, "settings": "ok"}},
        {"files": [], "summary": {**SUMMARY, "settings": "absent"}},
        # knowledge_roots, when present, must be a list of strings.
        {"files": [], "summary": {**SUMMARY, "knowledge_roots": "Library"}},
        {"files": [], "summary": {**SUMMARY, "knowledge_roots": [1]}},
        {"files": [], "summary": {**SUMMARY, "knowledge_roots": None}},
    ],
)
async def test_a_manifest_with_a_bad_class_or_summary_is_refused(stub, body) -> None:
    stub.respond("GET", "/v1/manifest", 200, body)
    with pytest.raises(VaultError) as exc:
        await _client(stub).manifest()
    assert exc.value.code == errors.BAD_RESPONSE


async def test_a_wrong_case_settings_state_parses(stub) -> None:
    """8f: vaultd's fourth settings state, and the default when a build
    predating it simply omits `knowledge_roots`."""
    stub.respond(
        "GET", "/v1/manifest", 200, {"files": [], "summary": {**SUMMARY, "settings": "wrong_case"}}
    )
    manifest = await _client(stub).manifest()
    assert manifest.summary.settings == "wrong_case"
    assert manifest.summary.knowledge_roots == ()


async def test_a_file_with_a_foreign_class_is_refused(stub) -> None:
    stub.respond("GET", "/v1/file", 200, {"path": "Бег.md", "sha256": "a" * 64, "content": "x", "class": "never"})
    with pytest.raises(VaultError) as exc:
        await _client(stub).get_file("Бег.md")
    assert exc.value.code == errors.BAD_RESPONSE


# --- knowledge routes (W2b) ---------------------------------------------


async def test_get_knowledge_parses_content_and_hash(stub) -> None:
    stub.respond("GET", "/v1/knowledge", 200, {"path": "Library/CCRU.md", "sha256": "b" * 64, "content": "text"})
    result = await _client(stub).get_knowledge("Library/CCRU.md")
    assert result.path == "Library/CCRU.md"
    assert result.sha256 == "b" * 64
    assert result.content == "text"
    assert stub.calls() == [("GET", "/v1/knowledge")]


async def test_get_knowledge_404_is_not_found(stub) -> None:
    stub.respond("GET", "/v1/knowledge", 404, {"error": "not_found"})
    with pytest.raises(VaultError) as exc:
        await _client(stub).get_knowledge("Library/CCRU.md")
    assert exc.value.code == errors.NOT_FOUND


async def test_put_knowledge_sends_changeset_and_returns_hash(stub) -> None:
    stub.respond("PUT", "/v1/knowledge", 200, {"sha256": "c" * 64, "folders_created": 2})
    result = await _client(stub).put_knowledge("Library/CCRU.md", "new text", "a" * 64, "chg1")
    assert result.sha256 == "c" * 64
    assert result.folders_created == 2
    assert stub.calls() == [("PUT", "/v1/knowledge")]


async def test_put_knowledge_403_empty_body_is_refused(stub) -> None:
    stub.respond("PUT", "/v1/knowledge", 403, b"")
    with pytest.raises(VaultError) as exc:
        await _client(stub).put_knowledge("Library/CCRU.md", "x", "a" * 64, "chg1")
    assert exc.value.code == errors.REFUSED


async def test_put_knowledge_412_is_conflict(stub) -> None:
    stub.respond("PUT", "/v1/knowledge", 412, {"error": "precondition_failed"})
    with pytest.raises(VaultError) as exc:
        await _client(stub).put_knowledge("Library/CCRU.md", "x", "a" * 64, "chg1")
    assert exc.value.code == errors.CONFLICT


async def test_rename_knowledge_parses_relinked(stub) -> None:
    stub.respond(
        "POST", "/v1/knowledge/rename", 200,
        {
            "path": "Library/New.md", "sha256": "d" * 64, "relinked": 2,
            "folders_created": 1, "files_moved": 3,
        },
    )
    result = await _client(stub).rename_knowledge("Library/Old.md", "Library/New.md", "a" * 64, "chg1")
    assert result.path == "Library/New.md"
    assert result.sha256 == "d" * 64
    assert result.relinked == 2
    assert result.folders_created == 1
    assert result.files_moved == 3


async def test_list_changes_parses_entries(stub) -> None:
    stub.respond(
        "GET", "/v1/changes", 200,
        {
            "changes": [
                {
                    "id": "chg1",
                    "kind": "write",
                    "time": "2026-09-27T10:00:00Z",
                    "undone": False,
                    "files": [{"path": "Library/CCRU.md", "sha256": "e" * 64}],
                }
            ]
        },
    )
    changes = await _client(stub).list_changes()
    assert len(changes) == 1
    assert changes[0].id == "chg1"
    assert changes[0].kind == "write"
    assert changes[0].undone is False
    assert changes[0].files[0].path == "Library/CCRU.md"
    assert changes[0].files[0].sha256 == "e" * 64


async def test_undo_changeset_parses_counts(stub) -> None:
    stub.respond("POST", "/v1/undo", 200, {"restored": 2, "refused": 1})
    result = await _client(stub).undo_changeset("chg1")
    assert result.restored == 2
    assert result.refused == 1


# --- L1: the lens class and the graph (anchor-lens-plan.md sections 3-4) ------


async def test_a_lens_entry_carries_its_kind(stub) -> None:
    stub.respond(
        "GET",
        "/v1/manifest",
        200,
        {
            "files": [
                {**NOTE, "path": "Lens/Beer.md", "class": "lens", "lens_kind": "person"},
                {**NOTE, "path": "Lens/Variety.md", "class": "lens", "lens_kind": "concept"},
                {**NOTE, "path": "Library/CCRU.md", "class": "knowledge", "lens_kind": None},
            ],
            "summary": SUMMARY,
        },
    )
    entries = (await _client(stub).manifest()).entries
    assert [(e.note_class, e.lens_kind) for e in entries] == [
        ("lens", "person"),
        ("lens", "concept"),
        ("knowledge", None),
    ]


@pytest.mark.parametrize(
    "item",
    [
        {**NOTE, "class": "lens"},
        {**NOTE, "class": "lens", "lens_kind": "Person"},
        {**NOTE, "class": "lens", "lens_kind": None},
        {**NOTE, "class": "knowledge", "lens_kind": "person"},
        {**NOTE, "class": "personal", "lens_kind": "concept"},
    ],
)
async def test_a_lens_kind_where_it_does_not_belong_is_refused(stub, item) -> None:
    stub.respond("GET", "/v1/manifest", 200, {"files": [item], "summary": SUMMARY})
    with pytest.raises(VaultError) as exc:
        await _client(stub).manifest()
    assert exc.value.code == errors.BAD_RESPONSE


async def test_a_lens_file_is_accepted(stub) -> None:
    stub.respond("GET", "/v1/file", 200, {"path": "Lens/Beer.md", "sha256": "a" * 64, "content": "x", "class": "lens"})
    assert (await _client(stub).get_file("Lens/Beer.md")).note_class == "lens"


async def test_the_tree_marks_lens_notes(stub) -> None:
    stub.respond(
        "GET",
        "/v1/knowledge/tree",
        200,
        {
            "folders": ["Lens"],
            "notes": [
                {"path": "Lens/Beer.md", "title": "Beer", "class": "lens"},
                {"path": "Library/CCRU.md", "title": "CCRU", "class": "knowledge"},
                {"path": "Library/Old.md", "title": "Old"},
            ],
            "truncated": False,
        },
    )
    tree = await _client(stub).knowledge_tree()
    assert [n.note_class for n in tree.notes] == ["lens", "knowledge", "knowledge"]

    stub.respond(
        "GET",
        "/v1/knowledge/tree",
        200,
        {"folders": [], "notes": [{"path": "Life/X.md", "title": "X", "class": "personal"}], "truncated": False},
    )
    with pytest.raises(VaultError):
        await _client(stub).knowledge_tree()


GRAPH_NODE = {
    "path": "Lens/Beer.md",
    "title": "Beer",
    "class": "lens",
    "lens_kind": "person",
    "aliases": ["Stafford Beer"],
    "tags": ["cybernetics"],
    "summary": "Management cybernetics.",
    "chars": 120,
}


async def test_the_graph_parses_nodes_and_every_edge_shape(stub) -> None:
    stub.respond(
        "GET",
        "/v1/knowledge/graph",
        200,
        {
            "nodes": [
                GRAPH_NODE,
                {
                    "path": "Library/CCRU.md", "title": "CCRU", "class": "knowledge", "lens_kind": None,
                    "aliases": [], "tags": [], "summary": "x" * 400, "chars": 5,
                },
            ],
            "edges": [
                {"src": "Lens/Beer.md", "dst": "Library/CCRU.md"},
                {"src": "Lens/Beer.md", "unresolved": "Viable system model"},
                {"src": "Library/CCRU.md", "outside": True},
            ],
            "truncated": True,
        },
    )
    graph = await _client(stub).knowledge_graph()
    beer, ccru = graph.nodes
    assert (beer.note_class, beer.lens_kind, beer.aliases, beer.tags) == (
        "lens", "person", ("Stafford Beer",), ("cybernetics",),
    )
    assert ccru.lens_kind is None and len(ccru.summary) == client_module.GRAPH_SUMMARY_MAX_CHARS
    assert [(e.dst, e.unresolved, e.outside) for e in graph.edges] == [
        ("Library/CCRU.md", None, False),
        (None, "Viable system model", False),
        (None, None, True),
    ]
    assert graph.truncated is True
    assert stub.requests[-1].authorization == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "body",
    [
        # An outside link that names its note is refused whole.
        {"nodes": [], "edges": [{"src": "a.md", "outside": True, "dst": "Life/Private.md"}], "truncated": False},
        {"nodes": [], "edges": [{"src": "a.md", "outside": True, "unresolved": "Private"}], "truncated": False},
        {"nodes": [], "edges": [{"src": "a.md", "outside": True, "title": "Private"}], "truncated": False},
        {"nodes": [], "edges": [{"src": "a.md", "outside": False}], "truncated": False},
        # Exactly one target.
        {"nodes": [], "edges": [{"src": "a.md"}], "truncated": False},
        {"nodes": [], "edges": [{"src": "a.md", "dst": "b.md", "unresolved": "b"}], "truncated": False},
        # Only knowledge and lens nodes, lens with a kind.
        {"nodes": [{**GRAPH_NODE, "class": "personal", "lens_kind": None}], "edges": [], "truncated": False},
        {"nodes": [{**GRAPH_NODE, "lens_kind": None}], "edges": [], "truncated": False},
        {"nodes": [{**GRAPH_NODE, "aliases": "Stafford Beer"}], "edges": [], "truncated": False},
        {"nodes": [{**GRAPH_NODE, "chars": -1}], "edges": [], "truncated": False},
        {"nodes": [], "edges": []},
    ],
)
async def test_a_malformed_or_revealing_graph_is_refused(stub, body) -> None:
    stub.respond("GET", "/v1/knowledge/graph", 200, body)
    with pytest.raises(VaultError) as exc:
        await _client(stub).knowledge_graph()
    assert exc.value.code == errors.BAD_RESPONSE
