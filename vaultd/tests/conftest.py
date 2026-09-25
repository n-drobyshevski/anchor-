"""Fixtures for vaultd's tests: a temp vault, a fake `ob`, an app client.

Nothing here touches the network or Obsidian. The fake `ob` is a small
Python script written into the test's temp directory; its answers come
from a JSON spec file next to it, and it appends each invocation's argv
and environment *names* (never values) to a calls file. The spec and
calls paths are baked into the script rather than passed through the
environment, because vaultd gives ob children an allowlisted
environment and would strip anything else.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from vaultd.api import make_app
from vaultd.config import Config
from vaultd.manifest import Manifest
from vaultd.store import Store

# Deliberately low-entropy, so a secret scanner never mistakes it.
TOKEN = "test-token-" + "x" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class StaticStatus:
    def __init__(self) -> None:
        self.value = {
            "sync_running": True,
            "restarts": 0,
            "last_exit_code": None,
            "running_since": "2026-09-25T10:00:00+00:00",
        }

    def snapshot(self) -> dict:
        return self.value


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "vault").mkdir(parents=True)
    (root / "tmp").mkdir()
    (root / "config").mkdir()
    return root


@pytest.fixture
def vault(data_dir: Path) -> Path:
    return data_dir / "vault"


@pytest.fixture
def store(data_dir: Path) -> Store:
    return Store(data_dir / "vault", data_dir / "tmp")


@pytest.fixture
def manifest(vault: Path) -> Manifest:
    return Manifest(vault)


@pytest.fixture
async def client(aiohttp_client, store: Store, manifest: Manifest):
    app = make_app(token=TOKEN, store=store, manifest_=manifest, status_source=StaticStatus())
    return await aiohttp_client(app)


def write(root: Path, rel: str, content: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


class FakeOb:
    """Controls and inspects the fake `ob` script."""

    def __init__(self, directory: Path) -> None:
        self.bin = directory / "ob"
        self.spec_path = directory / "ob-spec.json"
        self.calls_path = directory / "ob-calls.jsonl"
        self.spec: dict = {}
        self.save()
        self.bin.write_text(
            f"#!{sys.executable}\n"
            + _FAKE_OB_TEMPLATE.format(spec=str(self.spec_path), calls=str(self.calls_path))
        )
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)

    def save(self) -> None:
        self.spec_path.write_text(json.dumps(self.spec))

    def set(self, command: str, **behaviour) -> None:
        self.spec[command] = behaviour
        self.save()

    def calls(self) -> list[dict]:
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def commands(self) -> list[str]:
        return [call["argv"][0] for call in self.calls()]


_FAKE_OB_TEMPLATE = r'''
import json, os, sys, time
with open({spec!r}) as f:
    spec = json.load(f)
argv = sys.argv[1:]
with open({calls!r}, "a") as f:
    f.write(json.dumps({{"argv": argv, "env": sorted(os.environ)}}) + "\n")
behaviour = spec.get(argv[0] if argv else "", {{}})
out = behaviour.get("stdout")
if out is not None:
    sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
    sys.stdout.flush()
err = behaviour.get("stderr")
if err is not None:
    sys.stderr.write(err)
    sys.stderr.flush()
time.sleep(behaviour.get("sleep", 0))
sys.exit(behaviour.get("exit", 0))
'''


@pytest.fixture
def fake_ob(tmp_path: Path) -> FakeOb:
    directory = tmp_path / "bin"
    directory.mkdir()
    return FakeOb(directory)


OWNED_ID = "a1b2c3d4e5f6"
SHARED_ID = "ffeeddccbbaa"


@pytest.fixture
def boot_env(data_dir: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(data_dir),
        "VAULT_API_TOKEN": TOKEN,
        "OBSIDIAN_AUTH_TOKEN": "fake-auth-token",
        "OBSIDIAN_VAULT": "Anchor",
        "OBSIDIAN_E2EE_PASSWORD": "fake-e2ee-password",
        "VAULT_PATH": str(data_dir / "vault"),
        "XDG_CONFIG_HOME": str(data_dir / "config"),
    }


def boot_config(env: dict[str, str]) -> Config:
    from vaultd.config import from_env

    return from_env(env)


def remote_listing(owned=(("Anchor", OWNED_ID),), shared=()) -> dict:
    return {
        "vaults": [{"id": i, "name": n, "region": "eu"} for n, i in owned],
        "shared": [{"id": i, "name": n, "region": "eu"} for n, i in shared],
    }
