# Echo (formerly Anchor): rules for Claude Code

- Never read the user's conversation data: message text, update
  payloads, memory, journal, summaries, check-in notes. Do not try to
  obtain the production database URL, the bot token, the OpenRouter key
  or `.env`, and do not call the Telegram Bot API. A PreToolUse hook
  enforces this; do not work around it.
- Debug production with Railway logs, deployments and metrics, and with
  SQL on the content-free views only:
  `psql "$ANCHOR_DEBUG_DATABASE_URL" -c "select ... from debug.<table>"`.
  See docs/claude-access.md.
- Tests and eval run against a throwaway local database (see README →
  Tests); synthetic data there is fine to read.
- Logs must never carry message text (app/log.py); keep it that way.
- Never read the Obsidian vault, except lens notes, through the
  `anchor_lens` role (next bullet), and notes inside a code window the
  user opened (the bullet after it), and never connect Obsidian tools to
  it (MCP servers, the Local REST API, `ob`). It holds the same data as
  the database. vaultd's tests use a temp directory and a fake `ob`; the
  vault's credentials (`VAULT_API_TOKEN`, `OBSIDIAN_*`) are secrets like
  the others above.
- Lens notes may be read, through
  `psql "$ANCHOR_LENS_DATABASE_URL" -c "select ... from lens.<fn>()"`
  (`lens.notes()`, `lens.graph()`, `lens.rounds(n)`, `lens.gaps(n)`).
  `lens.rounds(n)` returns which notes each weekly review or reflection round picked
  and its outcome, never the rationale (written from the week, so it
  stays with the user). `lens.gaps(n)` returns the weekly lens garden's
  proposals (written from lens notes only), with their status as
  open, done, dismissed or closed. Every call is logged in `lens_read`
  and counted in the user's daily digest; use plain autocommit `-c`,
  never a transaction you roll back (that is still detected, and shown
  to the user as a read without a record). Lens text never leaves the
  session: not in commits, PR text, code comments, test fixtures, eval
  cases, logs or artifacts. Paraphrase the idea from public knowledge
  ("Ashby's requisite variety") and cite the lens note by id.
  Everything else in the vault stays off limits, as above. See
  docs/claude-access.md → Lens notes.
- Other vault notes may be read only while the user has opened a
  **code window** (`/vault code on` in Telegram), and only through
  `curl -sS -X POST "$ANCHOR_CODE_URL/status"`, `/tree` or `/file`
  (JSON bodies; never a path in a URL). A 403 `no_window` means the
  door is closed: ask the user to open a window, and say which folders
  the task needs; never look for another way in, and never ask for
  more than the task needs. Read only the notes the task needs, not
  the whole tree's text. `Anchor/` (memory, journal, reports) and
  `never` notes stay closed in every window. Every read is counted
  and shown to the user. Note text read this way never leaves the
  session, exactly like lens text (previous bullet): not in commits,
  PR text, code comments, test fixtures, eval cases, logs or
  artifacts. See docs/claude-access.md → The code window.
- Never call Anchor's own connector, even if its tools appear in your
  session (`mcp__Anchor__...`, `mcp__claude_ai_Anchor__...`, the same
  under the new name Echo (`mcp__Echo__...`), or any
  tool named `get_memory`, `get_journal`, `get_dialogs`, `get_state`,
  `search_library`, `update_note`, `create_note`, `rename_note`,
  `get_note`, `list_changes`, `undo_changeset`, `list_tree`): it
  returns the same conversations as the database, and the last seven
  can write to (or, for `list_tree`, see the structure of) the user's
  vault -- you are building them, not calling them.
  Never read Railway's `http` log stream either: request paths carry
  Grok's capability token. The guard hook blocks both; do not work
  around it.
- Visual and UI work (web UI, mark, avatar) starts from
  docs/brand/README.md.
