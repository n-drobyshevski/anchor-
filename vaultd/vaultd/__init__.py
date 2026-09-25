"""vaultd: the vault service's own process (phase-8 plan section 5).

The vault service runs two things under one supervisor: `ob sync
--continuous`, which keeps /data/vault in step with Obsidian Sync, and
this package's small HTTP API, which is the only way the bot can touch
those files.

**vaultd is the enforcement point, not the bot.** The same shape as the
`anchor_debug` role: whatever the bot's code does, *this* process
refuses a write outside `Anchor/Memory/` and `Anchor/Journal/`, and a
read of any note the user has not classified `personal` or `knowledge`
(8e). The path rules live in `paths.py`, a note's own mark in
`frontmatter.py`, the folder rules and precedence in `classes.py`, and
compare-and-swap in `store.py`; `tests/` pins each of them with hostile
inputs, independently of anything the bot does.

**Independence.** This is a separate project with its own
pyproject.toml, lockfile and tests. It imports nothing from `app`, and
`app` imports nothing from here -- pinned by an AST test on both sides
-- so the security boundary cannot quietly start depending on the code
it is meant to constrain.
"""
