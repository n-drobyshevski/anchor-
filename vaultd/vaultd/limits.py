"""User-set caps on Claude's knowledge writes, persisted outside the vault.

The rate/volume caps in config.py (`FILES_PER_CHANGESET`,
`CHANGESETS_PER_HOUR`, ...) are now defaults, not fixed values: the
user tunes them by hand from Telegram or the web app, the bot keeps
the source of truth and pushes the values here with `PUT /v1/limits`
(docs/decisions.md, "Claude write caps become settings"). Each value
is held to its `SPECS` bounds, so no push can set a nonsense number.

What stays constant: the byte cap per note, folder depth and the undo
TTL -- those touch vaultd's body/read caps and its storage, and are
not in `SPECS`.

**Storage.** One JSON object at `<undo_root>/limits.json`: outside the
vault (boot.py refuses an undo root inside it), so `ob` never syncs
it. Written atomically (temp file + `os.replace`). A missing or
unreadable file reads as the defaults -- the same values vaultd always
used -- and a key it does not know is dropped, never trusted.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from vaultd.config import (
    CHANGESETS_PER_HOUR,
    FILES_PER_CHANGESET,
    FOLDERS_PER_CHANGESET,
    FOLDERS_PER_DAY,
    MOVE_FILES_PER_CHANGESET,
    MOVES_PER_DAY,
    UNDOS_PER_HOUR,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Spec:
    default: int
    min: int
    max: int


# Keys match the bot's (app/core/claude_write_limits.py); the bot also
# has two caps vaultd never sees (creates per day, bytes per day).
SPECS: dict[str, Spec] = {
    "files_per_changeset": Spec(FILES_PER_CHANGESET, 1, 200),
    "changesets_per_hour": Spec(CHANGESETS_PER_HOUR, 0, 60),
    "undos_per_hour": Spec(UNDOS_PER_HOUR, 0, 60),
    "folders_per_changeset": Spec(FOLDERS_PER_CHANGESET, 0, 20),
    "folders_per_day": Spec(FOLDERS_PER_DAY, 0, 100),
    "move_files_per_changeset": Spec(MOVE_FILES_PER_CHANGESET, 1, 200),
    "moves_per_day": Spec(MOVES_PER_DAY, 0, 600),
}


@dataclass(frozen=True)
class Limits:
    files_per_changeset: int = FILES_PER_CHANGESET
    changesets_per_hour: int = CHANGESETS_PER_HOUR
    undos_per_hour: int = UNDOS_PER_HOUR
    folders_per_changeset: int = FOLDERS_PER_CHANGESET
    folders_per_day: int = FOLDERS_PER_DAY
    move_files_per_changeset: int = MOVE_FILES_PER_CHANGESET
    moves_per_day: int = MOVES_PER_DAY

    def as_json(self) -> dict[str, int]:
        return asdict(self)


DEFAULTS = Limits()


class Invalid(ValueError):
    """A limits body with an unknown key, a non-int or an out-of-range value."""


def _valid_value(key: str, value: Any) -> bool:
    spec = SPECS[key]
    # bool is an int subclass; `true` is not a cap.
    return isinstance(value, int) and not isinstance(value, bool) and spec.min <= value <= spec.max


def validate(body: Any, base: Limits = DEFAULTS) -> Limits:
    """`base` with `body`'s keys applied. Raises Invalid on anything off."""
    if not isinstance(body, dict):
        raise Invalid("not an object")
    for key, value in body.items():
        if key not in SPECS:
            raise Invalid(f"unknown key {key!r}")
        if not _valid_value(key, value):
            raise Invalid(f"bad value for {key!r}")
    return Limits(**{**base.as_json(), **body})


class LimitsStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def get(self) -> Limits:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return DEFAULTS
        except (OSError, ValueError):
            logger.warning("limits_unreadable", extra={"event": "limits_unreadable"})
            return DEFAULTS
        if not isinstance(raw, dict):
            logger.warning("limits_unreadable", extra={"event": "limits_unreadable"})
            return DEFAULTS
        # Keep only known, in-range keys: a stale or hand-edited file
        # falls back per key rather than all-or-nothing.
        clean = {k: v for k, v in raw.items() if k in SPECS and _valid_value(k, v)}
        return Limits(**{**DEFAULTS.as_json(), **clean})

    def put(self, limits: Limits) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(limits.as_json()))
        os.replace(tmp, self.path)

    def reset(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
