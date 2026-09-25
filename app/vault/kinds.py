"""The vault's job kinds and dedup keys, in a module that imports nothing.

app/core/purge.py queues `vault_purge` inside /delete's transaction, and
app/worker.py dispatches both kinds; neither should have to import the
sync pass (and aiohttp with it) just for a string.
"""

from __future__ import annotations

import datetime

VAULT_SYNC = "vault_sync"
VAULT_PURGE = "vault_purge"

# One purge at a time, ever: a second /delete while the first purge is
# still retrying collapses into the job already queued.
VAULT_PURGE_DEDUP_KEY = "vault_purge"

# The modes in which a sync pass runs at all.
SYNC_MODES = ("mirror", "sync")


def sync_dedup_key(now_utc: datetime.datetime) -> str:
    """One pass per UTC minute (phase-5 plan section 7)."""
    return f"{VAULT_SYNC}:{now_utc.astimezone(datetime.timezone.utc):%Y-%m-%dT%H:%M}"
