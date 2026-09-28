"""Bot-side caps on Claude's writes, mirroring vaultd's own copy
(anchor-claude-write-plan.md section 6.4, section 14 (rev. 3);
vaultd/vaultd/config.py and vaultd/vaultd/undo.py hold vaultd's copy).

Constants, not settings: a deploy must not be able to loosen these by
pasting a variable, the same rule vaultd's own config.py states for its
copy. All caps except `CREATES_PER_DAY`/`BYTES_PER_CONNECTION_PER_DAY`/
`CHANGESETS_PER_HOUR`/`UNDOS_PER_HOUR`/`FOLDERS_PER_DAY`/`MOVES_PER_DAY`
are checked directly against an argument or a `claude_changeset` row
already in hand; those are enforced by summing `claude_changeset`
(app/web/claude_write.py) -- ledger-backed, not an in-memory tracker,
so a worker restart cannot loosen them.

**FILES_PER_CHANGESET covers content writes only** (`update_note`,
`create_note`) -- rev. 3 splits a rename's own files off into
`MOVE_FILES_PER_CHANGESET`/`MOVES_PER_DAY`, entirely separate. The bot
cannot know in advance how many folders a nested `create_note`/
`rename_note` will need (vaultd resolves the tree), so
`FOLDERS_PER_CHANGESET`/`FOLDERS_PER_DAY` and the move caps are
enforced authoritatively by vaultd itself (app/vault/client.py's
`folders_created`/`files_moved`); the bot's own check against its
ledger, before ever calling vaultd, is a fast local rejection once a
budget is already exhausted -- the same belt-and-suspenders shape as
every other mirrored cap here.
"""

from __future__ import annotations

import datetime

FILES_PER_CHANGESET = 5
CHANGESETS_PER_HOUR = 4
CREATES_PER_DAY = 10
BYTES_PER_FILE = 64 * 1024
BYTES_PER_CONNECTION_PER_DAY = 512 * 1024
UNDOS_PER_HOUR = 4

# Rev. 3 (anchor-claude-write-plan.md section 14): folder auto-creation
# and the move budget, mirroring vaultd's own copy exactly.
FOLDERS_PER_CHANGESET = 3
FOLDERS_PER_DAY = 10
MAX_FOLDER_DEPTH = 4
MOVE_FILES_PER_CHANGESET = 20
MOVES_PER_DAY = 60

# A changeset is all writes by one connection within this idle window
# (plan section 6.3): reuse the open one if the last write was under
# this long ago, else mint a new one.
CHANGESET_IDLE = datetime.timedelta(minutes=10)
