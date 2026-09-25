"""vaultd's configuration: a handful of environment variables and constants.

The environment carries only what differs per deploy -- credentials,
the vault's name, paths. Every limit that protects the vault is a code
constant instead, so no deploy (and no pasted variable) can widen it:
the body cap, the frontmatter cap, the note size cap and the restart
backoff are all here, not in the environment.

Reading the environment never validates it; `boot.check_env` does that,
and names a variable without ever echoing its value.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# A note larger than this is never listed, read or indexed. The bot's
# later VAULT_NOTE_MAX_BYTES (8d) can only narrow it: vaultd decides
# what is readable, and a bot setting must not be able to widen that.
NOTE_MAX_BYTES = 200_000

# Plan section 4.4: a single leading `---` fence, at most 4 KB. Beyond
# that the file is treated as having no frontmatter at all -- which for
# the opt-in check means "not opted in".
FRONTMATTER_MAX_BYTES = 4096

# Plan section 5.4: a PUT body is at most 64 KB. A fact file or a day
# of journal is a few KB; anything near this is a bug.
BODY_MAX_BYTES = 64 * 1024

# Supervisor backoff (plan section 5.3): 5 s doubling to 5 min. The
# 5 s floor also clears ob's own lock, which it treats as stale 5 s
# after its last refresh (see docs/decisions.md).
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
# A child that ran this long before exiting was healthy; the next
# failure starts the backoff from the floor again rather than from
# wherever an old crash loop left it.
STABLE_RUN_S = 300.0

# Each boot-time `ob` command (list, setup, config) gets this long. It
# talks to Obsidian's API once; two minutes is generous.
OB_COMMAND_TIMEOUT_S = 120.0

# Where the Dockerfile's `npm ci` puts the pinned binary. Tests pass a
# fake script instead.
OB_BIN = "/app/node_modules/.bin/ob"

DEFAULT_VAULT_PATH = "/data/vault"
DEFAULT_CONFIG_HOME = "/data/config"
DEFAULT_DEVICE_NAME = "anchor-railway"
DEFAULT_PORT = 8080


@dataclass(frozen=True)
class Config:
    api_token: str
    auth_token: str
    vault: str
    e2ee_password: str
    device_name: str
    vault_path: Path
    config_home: Path
    port: int

    @property
    def tmp_path(self) -> Path:
        """Temp files live beside the vault, on the same volume.

        `os.link` and `os.replace` are only atomic within one
        filesystem, so the temp directory must never be /tmp.
        """
        return self.vault_path.parent / "tmp"


def from_env(env: Mapping[str, str]) -> Config:
    """Read the environment. Raises ValueError only for a non-numeric PORT."""

    def get(name: str, default: str = "") -> str:
        return env.get(name, default).strip()

    port_raw = get("PORT", str(DEFAULT_PORT))
    if not port_raw.isdigit():
        raise ValueError("PORT must be a number")
    return Config(
        api_token=get("VAULT_API_TOKEN"),
        auth_token=get("OBSIDIAN_AUTH_TOKEN"),
        vault=get("OBSIDIAN_VAULT"),
        e2ee_password=env.get("OBSIDIAN_E2EE_PASSWORD", ""),
        device_name=get("OB_DEVICE_NAME", DEFAULT_DEVICE_NAME) or DEFAULT_DEVICE_NAME,
        vault_path=Path(get("VAULT_PATH", DEFAULT_VAULT_PATH) or DEFAULT_VAULT_PATH),
        config_home=Path(get("XDG_CONFIG_HOME", DEFAULT_CONFIG_HOME) or DEFAULT_CONFIG_HOME),
        port=int(port_raw),
    )
