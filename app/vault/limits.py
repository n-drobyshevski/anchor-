"""Constants that gate how fast and how far the vault may change memory.

Phase-8 plan section 3 lists these as environment variables
(`VAULT_DELETE_GRACE_S`, `VAULT_SYNC_WARMUP_S`, `VAULT_MASS_DELETE_MAX`,
`VAULT_HOLD_TTL_DAYS`). They are deliberately *not* on `Settings` here:
each one is a floor or ceiling on a destructive or bulk action --
forgetting facts, accepting a rule, waiting out a half-synced client --
and a deploy must not be able to loosen any of them by pasting a wider
value into the environment. Same call as docs/decisions.md's "8a --
limits that are constants, not settings" (vaultd's own caps) and
app/core/memory.py's RETRIEVAL_MIN_SCORE/DEDUPE_MAX_SIMILARITY.

`FACT_MAX_CHARS` matches app/core/memory.py's `MEMORY_TEXT_MAX` (both
300) but is defined here rather than imported from it: app/vault/ must
not depend on which module happens to hold the number today, and the
schema's own `ck_memory_text_length` is the ultimate backstop either
one would hit if the two ever drifted.
"""

from __future__ import annotations

import datetime

DELETE_GRACE_S = 600
"""A vanished fact or journal file counts as deleted only once it has
been missing from the manifest for this long (plan section 7.2):
Obsidian Sync can deliver a rename as a delete now and a create minutes
later."""

SYNC_WARMUP_S = 300
"""No deletions apply until `ob` has been running continuously for at
least this long (plan section 7.2): a just-restarted sync may still be
half-way through downloading the vault, and a half-synced manifest must
not read as "you deleted this"."""

MASS_DELETE_MAX = 3
"""More forgets than this from the vault within `MASS_DELETE_WINDOW`
open one `mass_delete` hold instead of forgetting outright (plan
section 7.2)."""

MASS_DELETE_WINDOW = datetime.timedelta(hours=1)
"""The rolling window `MASS_DELETE_MAX` counts over."""

HOLD_TTL_DAYS = 7
"""An unanswered hold resolves as its revert action after this many
days (plan section 8): anything short of a clean yes is a no, and the
no is always the direction where nothing is lost."""

FACT_MAX_CHARS = 300
"""A fact file's `fact` property, after stripping and collapsing
whitespace, must fit this to be ingested (plan section 7.1) -- the same
cap every other writer of a memory's text is held to."""
