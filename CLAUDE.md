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
