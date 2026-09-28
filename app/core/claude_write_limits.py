"""Bot-side caps on Claude's writes, mirroring vaultd's own copy
(anchor-claude-write-plan.md section 6.4; vaultd/vaultd/config.py and
vaultd/vaultd/undo.py hold vaultd's copy).

Constants, not settings: a deploy must not be able to loosen these by
pasting a variable, the same rule vaultd's own config.py states for its
copy. All caps except `CREATES_PER_DAY`/`BYTES_PER_CONNECTION_PER_DAY`/
`CHANGESETS_PER_HOUR`/`UNDOS_PER_HOUR` are checked directly against an
argument or a `claude_changeset` row already in hand; those four are
enforced by summing `claude_changeset` (app/web/claude_write.py) --
ledger-backed, not an in-memory tracker, so a worker restart cannot
loosen them.
"""

from __future__ import annotations

import datetime

FILES_PER_CHANGESET = 5
CHANGESETS_PER_HOUR = 4
CREATES_PER_DAY = 10
BYTES_PER_FILE = 64 * 1024
BYTES_PER_CONNECTION_PER_DAY = 512 * 1024
UNDOS_PER_HOUR = 4

# A changeset is all writes by one connection within this idle window
# (plan section 6.3): reuse the open one if the last write was under
# this long ago, else mint a new one.
CHANGESET_IDLE = datetime.timedelta(minutes=10)
