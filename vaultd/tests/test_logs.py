"""No log record carries a path, a file name or content (plan sections 5.3, 10, 15)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from vaultd import boot
from vaultd.log import JsonFormatter
from vaultd.supervisor import Supervisor
from tests.conftest import AUTH, OWNED_ID, boot_config, remote_listing, write

NOTE_TITLE = "Секретная заметка"
NOTE_BODY = "мой дневник о здоровье"


def _everything_logged(caplog) -> str:
    formatter = JsonFormatter()
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(str(record.__dict__))
        parts.append(formatter.format(record))
    return "\n".join(parts)


async def test_a_chatty_ob_leaves_no_file_name_in_the_logs(fake_ob, data_dir: Path, caplog) -> None:
    fake_ob.set(
        "sync",
        stdout=f"Uploading {NOTE_TITLE}.md\n",
        stderr=f"Error: conflict in Notes/{NOTE_TITLE}.md\n",
        exit=1,
    )
    calls = 0

    async def sleep(delay: float) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    sup = Supervisor([str(fake_ob.bin), "sync"], {"PATH": "/usr/bin:/bin"}, data_dir / "config", sleep=sleep)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(asyncio.CancelledError):
            await sup.run()
    logged = _everything_logged(caplog)
    assert "ob sync exited" in logged
    assert NOTE_TITLE not in logged


async def test_boot_logs_no_vault_name_or_ob_output(boot_env, fake_ob, data_dir: Path, caplog) -> None:
    boot_env["OBSIDIAN_VAULT"] = NOTE_TITLE
    fake_ob.set("sync-list-remote", stdout=remote_listing(owned=((NOTE_TITLE, OWNED_ID),)))
    fake_ob.set("sync-list-local", stdout={"vaults": []})
    fake_ob.set("sync-setup", stdout={"vaultName": NOTE_TITLE}, stderr=f"{NOTE_TITLE}.md")
    fake_ob.set("sync-config", stdout={"vaultName": NOTE_TITLE})
    with caplog.at_level(logging.DEBUG):
        await boot.boot(boot_config(boot_env), boot_env, str(fake_ob.bin))
    logged = _everything_logged(caplog)
    assert NOTE_TITLE not in logged
    assert "fake-e2ee-password" not in logged
    assert str(data_dir) not in logged


async def test_api_logs_carry_the_route_template_not_the_path(client, vault: Path, caplog) -> None:
    write(vault, f"{NOTE_TITLE}.md", f"---\nanchor: read\n---\n{NOTE_BODY}\n")
    write(vault, "Anchor/Memory/0001-abcdef.md", NOTE_BODY)
    with caplog.at_level(logging.DEBUG):
        await client.get("/v1/manifest", headers=AUTH)
        await client.get("/v1/file", params={"path": f"{NOTE_TITLE}.md"}, headers=AUTH)
        await client.get("/v1/file", params={"path": "missing-" + NOTE_TITLE + ".md"}, headers=AUTH)
        await client.put(
            "/v1/file",
            params={"path": f"Anchor/Memory/{NOTE_TITLE}.md"},
            json={"content": NOTE_BODY, "if_sha256": None},
            headers=AUTH,
        )
        await client.get("/v1/file", params={"path": f"{NOTE_TITLE}.md"})
        await client.get(f"/nowhere/{NOTE_TITLE}", headers=AUTH)
        await client.post("/v1/purge", headers=AUTH)
    logged = _everything_logged(caplog)
    assert NOTE_TITLE not in logged
    assert NOTE_BODY not in logged
    assert "0001-abcdef" not in logged
    routes = {r.__dict__.get("route") for r in caplog.records if r.getMessage() == "request"}
    assert routes == {"/v1/manifest", "/v1/file", "/v1/purge", "unmatched"}


def test_the_formatter_drops_keys_outside_the_allowlist() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "request", None, None)
    record.path = f"{NOTE_TITLE}.md"
    record.status = 200
    out = JsonFormatter().format(record)
    assert NOTE_TITLE not in out
    assert '"status": 200' in out


async def test_classification_logs_no_folder_title_or_class_per_path(client, vault: Path, caplog) -> None:
    folder = "Личная папка"
    write(vault, "Anchor/settings.md", f"---\nanchor: settings\npersonal_folders: [{folder}]\n---\n")
    write(vault, f"{folder}/{NOTE_TITLE}.md", f"---\nanchor: knowledge\n---\n{NOTE_BODY}\n")
    write(vault, f"{NOTE_TITLE}-2.md", f"---\nanchor: knowlege\n---\n{NOTE_BODY}\n")
    with caplog.at_level(logging.DEBUG):
        await client.get("/v1/manifest", headers=AUTH)
        await client.get("/v1/file", params={"path": f"{folder}/{NOTE_TITLE}.md"}, headers=AUTH)
        write(vault, "Anchor/settings.md", f"---\nanchor: settings\npersonal_folders: [{folder}\n---\n")
        await client.get("/v1/manifest", headers=AUTH)
        write(vault, "Anchor/settings.md", f"---\nanchor: settings\npersonal_folders: [{folder}]\n---\n")
        await client.get("/v1/manifest", headers=AUTH)
    logged = _everything_logged(caplog)
    assert "settings_invalid" in logged
    for secret in (folder, NOTE_TITLE, NOTE_BODY, "personal", "knowledge", "knowlege", "settings.md"):
        assert secret not in logged
