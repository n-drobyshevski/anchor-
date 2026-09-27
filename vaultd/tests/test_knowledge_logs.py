"""No log record from the knowledge routes carries a path, title or text
(write-plan section 4, 6.2; the same rule test_logs.py pins for the rest of vaultd)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from vaultd.log import JsonFormatter
from tests.conftest import AUTH, write

TITLE = "Секретный узел CCRU"
BODY = "Тайный текст о гиперстиции."
SETTINGS = f"---\nanchor: settings\nknowledge_folders: [{TITLE}]\n---\n"


def sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def note(mark: str = "knowledge", body: str = BODY + "\n") -> str:
    return f"---\nanchor: {mark}\n---\n{body}"


def _everything_logged(caplog) -> str:
    formatter = JsonFormatter()
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(str(record.__dict__))
        parts.append(formatter.format(record))
    return "\n".join(parts)


async def test_knowledge_write_rename_and_undo_are_logged_with_no_secret_in_any_record(
    client, vault: Path, caplog
):
    write(vault, "Anchor/settings.md", SETTINGS)
    (vault / TITLE).mkdir()
    old_path = f"{TITLE}/{TITLE}.md"
    new_path = f"{TITLE}/{TITLE}-2.md"
    write(vault, old_path, note())

    with caplog.at_level(logging.DEBUG):
        await client.put(
            "/v1/knowledge",
            params={"path": f"{TITLE}/new-{TITLE}.md"},
            json={"content": note(body="только что написано\n"), "if_sha256": None, "changeset": "cs"},
            headers=AUTH,
        )
        await client.get("/v1/knowledge", params={"path": old_path}, headers=AUTH)
        await client.get("/v1/knowledge", params={"path": "missing.md"}, headers=AUTH)
        await client.post(
            "/v1/knowledge/rename",
            json={"path": old_path, "new_path": new_path, "if_sha256": sha(note()), "changeset": "cs2"},
            headers=AUTH,
        )
        await client.get("/v1/changes", headers=AUTH)
        await client.post("/v1/undo", params={"changeset": "cs"}, headers=AUTH)
        await client.post("/v1/undo", params={"changeset": "cs2"}, headers=AUTH)
        await client.post("/v1/purge", headers=AUTH)

    logged = _everything_logged(caplog)
    for secret in (TITLE, BODY, "только что написано", old_path, new_path):
        assert secret not in logged

    routes = {r.__dict__.get("route") for r in caplog.records if r.getMessage() == "request"}
    assert routes >= {"/v1/knowledge", "/v1/knowledge/rename", "/v1/changes", "/v1/undo", "/v1/purge"}
    events = {r.__dict__.get("event") for r in caplog.records}
    assert {"knowledge_put", "knowledge_rename", "knowledge_undo"} <= events


async def test_a_refusal_still_logs_only_the_route_and_status(client, vault: Path, caplog):
    write(vault, "Anchor/settings.md", "---\nanchor: settings\n---\n")
    with caplog.at_level(logging.DEBUG):
        resp = await client.put(
            "/v1/knowledge",
            params={"path": f"{TITLE}/{TITLE}.md"},
            json={"content": note(), "if_sha256": None, "changeset": "cs"},
            headers=AUTH,
        )
    assert resp.status == 403
    logged = _everything_logged(caplog)
    assert TITLE not in logged
    statuses = {r.__dict__.get("status") for r in caplog.records if r.getMessage() == "request"}
    assert 403 in statuses
