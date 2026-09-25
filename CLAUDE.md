# Anchor: rules for Claude Code

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
- Never read the Obsidian vault, and never connect Obsidian tools to it
  (MCP servers, the Local REST API, `ob`). It holds the same data as the
  database. vaultd's tests use a temp directory and a fake `ob`; the
  vault's credentials (`VAULT_API_TOKEN`, `OBSIDIAN_*`) are secrets like
  the others above.
