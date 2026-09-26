# Privacy

Milestone 6e (Phase 6 plan section 9.5). English version of the exact
text `/privacy` shows in the bot (`app/tg/router.py`'s `PRIVACY_TEXT`,
Russian). Kept in sync by hand -- a deploy that changes the backup
retention figures or the clip-text retention window should update both.
tests/test_privacy.py checks that the two have the same number of lines
and that both name Obsidian (8e).

- What is stored, and where: messages, memory, notes, check-ins, the
  journal, standing orders and settings live in a Postgres database on
  Railway.
- Model calls go through OpenRouter with data collection set to deny;
  the model provider itself keeps data according to its own policies.
- If the planner is connected: the agenda (including a partner's shared
  events) and, with PLANNER_HEALTH, sleep and heart-rate metrics go into
  the model's prompt; the planner's tokens are stored in the database
  and left out of `/export`. If access is open (`/grok`, `/claude`),
  what is read goes to xAI or Anthropic; closing access stops further
  reads, not what was already read.
- Telegram chats are not end-to-end encrypted -- messages pass through
  Telegram's own servers and this bot's server.
- Database backups are encrypted (age) and kept as 14 daily plus 8
  weekly copies; older ones are deleted.
- Text of pages found through search is kept for 30 days, then erased
  -- the cards and links themselves stay.
- Obsidian notes are read only when you have marked them: personal ones
  only for the conversation with you, never for search or research;
  knowledge ones as reference material. In `sync` mode, editing or
  deleting a fact file in Anchor's folder changes Anchor's memory.
- Server logs hold only codes, counts and cost -- never text.
- `/export` downloads all of your own data as one file.
- `/delete` deletes all data and all backups, irreversibly.

## What this does not cover

This is the user-facing summary, not a legal document. For the actual
mechanics:

- what tables `/export` and `/delete` cover, and why a few (queue
  plumbing, an operational liveness marker) are deliberately excluded:
  `app/core/export.py`, `app/core/purge.py`, `tests/test_export.py`'s
  `NOT_EXPORTED`.
- what is logged, and the rule that no message text, prompt, completion
  or raw update payload is ever logged: `app/log.py`.
- how backups are encrypted and pruned: `app/ops/backup.py`, this
  directory's `restore.md`.
- retention sweeps (update payloads, terminal job rows, and messages
  once their scene has a summary): `app/core/retention.py`.
