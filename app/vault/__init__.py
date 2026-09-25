"""Anchor's side of the vault (phase-8 plan).

The vault itself lives on a separate Railway service; `vaultd` there is
the enforcement point, and everything in this package is a client of
it. 8a shipped the plumbing (the HTTP client, the epoch, and the
health probe behind `/vault` and `/state`), 8b renders facts and the
journal, and 8e adds notes consent (consent.py) and the only two
modules that touch note chunks (notes_personal.py, notes_knowledge.py;
tests/test_vault_notes_isolation.py pins who may import them).

**What this package may not touch (plan section 13), pinned by
tests/test_vault_isolation.py:** no LLM provider, no `update_state`, no
outbound module, no proposal, no persona loading and no `app.worker`.
The sync path makes no model call, and the vault can change no
`user_state` field ever -- except `notes_consent`, which only
consent.py writes, at the user's `/vault notes on|off`. (`vault_epoch`
is changed by `/delete`, not from here.) Nor does anything here import `vaultd`: the two
projects share an HTTP API and nothing else.
"""
