"""Anchor's side of the vault (phase-8 plan).

The vault itself lives on a separate Railway service; `vaultd` there is
the enforcement point, and everything in this package is a client of
it. 8a ships the plumbing only: the HTTP client, the epoch, and the
health probe behind `/vault` and `/state`. Nothing here reads or writes
a vault file yet -- that is 8b (render) and 8c (ingest).

**What this package may not touch (plan section 13), pinned by
tests/test_vault_isolation.py:** no LLM provider, no `update_state`, no
outbound module, no proposal, no persona loading and no `app.worker`.
The sync path makes no model call, and the vault can change no
`user_state` field ever -- `vault_epoch` is the one exception, and only
`/delete` changes it. Nor does anything here import `vaultd`: the two
projects share an HTTP API and nothing else.
"""
