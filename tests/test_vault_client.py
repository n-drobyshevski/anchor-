"""app/vault/client.py against a loopback stub (phase-5 plan section 5.4).

No network beyond 127.0.0.1, no vaultd, no Obsidian.
"""

from __future__ import annotations

import datetime
import socket

import pytest

from app.vault import client as client_module
from app.vault import errors
from app.vault.client import VaultClient
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
        {"files": [{"path": "Бег.md", "sha256": sha, "size": 10, "scope": "note"}]},
    )
    stub.respond("GET", "/v1/file", 200, {"path": "Бег.md", "sha256": sha, "content": "текст"})
    stub.respond("PUT", "/v1/file", 200, {"sha256": sha})
    stub.respond("DELETE", "/v1/file", 200, {"deleted": True})
    stub.respond("POST", "/v1/purge", 200, {"deleted": 3})
    client = _client(stub)

    [entry] = await client.manifest()
    assert (entry.path, entry.scope) == ("Бег.md", "note")
    got = await client.get_file("Бег.md")
    assert got.content == "текст"
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
