"""The vault epoch (phase-8 plan section 4).

Six random lowercase base32 characters, stored in
`user_state.vault_epoch`, set by the migration and replaced by
`/delete`. From 8b every file Anchor creates carries it, in its name
and in `anchor_epoch`.

It exists because `/delete` restarts identities. Without it, a phone
that was offline during a delete could re-upload the old `0001.md`
onto the path of a *new* fact #1, Sync would merge the two, and
deleted text would come back as an edit. With it, old-epoch files
never share a path with new ones, and are recognised as orphans.

`secrets`, not `random`: the value is not a secret, but it must not
repeat after a delete, and the stdlib's CSPRNG costs nothing extra.
"""

from __future__ import annotations

import re
import secrets

ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
LENGTH = 6
EPOCH_RE = re.compile(r"^[a-z2-7]{6}$")


def new_epoch() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
