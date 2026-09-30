"""vaultd's configuration: a handful of environment variables and constants.

The environment carries only what differs per deploy -- credentials,
the vault's name, paths. Every limit that protects the vault is a code
constant or a default instead, so no deploy (and no pasted variable)
can widen it: the body cap, the frontmatter cap, the note size cap and
the restart backoff are all here, not in the environment. The only
limits that change at runtime are Claude's write caps, set by the user
through the bot (limits.py).

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

# Plan (Claude writes knowledge notes) section 6.4, 6.2. The byte cap
# and the undo TTL are constants, by the same rule as the rest of this
# module. The rate/volume caps below (files and changesets, undos,
# folders, moves) are *defaults*: the user tunes them from Telegram or
# the web app, the bot pushes them to `PUT /v1/limits`, and limits.py
# holds each to its bounds. Never from the environment -- a deploy
# still cannot widen them by pasting a variable.
# FILES_PER_CHANGESET covers content writes only (rev. 3 splits renames
# off into their own MOVE_FILES_PER_CHANGESET/MOVES_PER_DAY budget).
KNOWLEDGE_WRITE_MAX_BYTES = 64 * 1024
FILES_PER_CHANGESET = 20
CHANGESETS_PER_HOUR = 4
UNDOS_PER_HOUR = 4
UNDO_TTL_DAYS = 14

# Rev. 3 (anchor-claude-write-plan.md section 14): vaultd may create
# missing folders on the way to a new note or a move target, only
# inside a folder already covered by a `knowledge_folders` rule, and
# only this far (constant)/this much (defaults, see limits.py):
FOLDER_MAX_DEPTH = 4
FOLDERS_PER_CHANGESET = 3
FOLDERS_PER_DAY = 10
# A rename's own budget -- the moved file plus every rewritten
# backlink -- entirely separate from FILES_PER_CHANGESET above.
MOVE_FILES_PER_CHANGESET = 20
MOVES_PER_DAY = 60
# GET /v1/knowledge/tree (rev. 3 BUILD item 4): at most this many notes
# listed, with `truncated: true` past it.
TREE_MAX_NOTES = 2000

# Lens L4 (anchor-lens-plan.md sections 9 and 14.5; the L4 spec section
# 5): Echo's own writer, `PUT /v1/echo/inbox`. Constants, never
# settings: the user tunes Claude's caps (limits.py), and none of
# those -- not even at 0 -- may block or loosen Echo's, which are
# counted only over changesets whose `meta.writer` is `echo`
# (undo.py). One adopted research result is one note, so one file per
# changeset; 20 a day sits well above what the shared /study quota
# (the bot's RESEARCH_JOBS_PER_DAY) can produce, plus replays. The
# default inbox is `Echo/Inbox` (classes.py decides when it
# applies); a name already taken there is retried as `name 2` up to
# `name 9`.
ECHO_FILES_PER_CHANGESET = 1
ECHO_CHANGESETS_PER_DAY = 20
ECHO_UNDOS_PER_HOUR = 4
ECHO_WRITE_MAX_BYTES = 32 * 1024
ECHO_NAME_SUFFIX_MAX = 9
ECHO_INBOX_DEFAULT = "Echo/Inbox"

DEFAULT_VAULT_PATH = "/data/vault"
DEFAULT_CONFIG_HOME = "/data/config"
DEFAULT_DEVICE_NAME = "anchor-railway"
DEFAULT_PORT = 8080
# Outside the vault root on purpose (plan section 6.2, 13.9): a
# directory `ob` never syncs. boot.py refuses to start if this ever
# resolves inside VAULT_PATH.
DEFAULT_UNDO_ROOT = "/data/anchor-undo"


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
    undo_root: Path

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
        undo_root=Path(get("VAULT_UNDO_ROOT", DEFAULT_UNDO_ROOT) or DEFAULT_UNDO_ROOT),
    )
