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
  knowledge ones as reference material, and with `/claude library on`
  Claude can search them (what it finds goes to Anthropic), and with
  `/claude library write on` change them (undo: `/claude undo`). Lens
  notes can be read by Claude Code while `/lens code on`
  (what it reads goes to Anthropic), together with
  which of them the weekly review or the notebook reflection picked, but not why: the
  review's explanation is written from your week and only you see it, and the reflection
  keeps none. While the lens is on, the weekly
  review sends the model the lens catalog (titles, summaries or the
  start of the text, links) and the notes it picks, whole, as reference
  material. While the lens is on in the notebook reflection, the model
  gets the lens catalog, Echo's draft changes to its working notes and
  the notes it picks, whole; only open threads are rephrased through
  the lens, never observations about you, and such threads stay in the
  notes the persona reads until they are closed or expire, even if the
  lens is turned off. The per-conversation reflection and the rating of
  replies send the model no lens text; which lens notes the rated
  replies rested on shows only in `/export`. While the lens garden is on, once a week the model gets the
  lens notes' titles, summaries or the start of the text, the links
  between them, and how many links each has to knowledge notes (never
  their titles); with `/lens code on` Claude Code can read its proposals
  too. If you tap «исследовать» under a garden gap, the model gets that
  gap and the titles and summaries of its notes, the search query goes
  to Exa (through OpenRouter), and pages come only from the sites in
  `PACKET_LENS`; what you accept («в Inbox») becomes a knowledge note in
  `Echo/Inbox`, which you can undo for 14 days (`/lens undo`). In `sync`
  mode, editing or
  deleting a fact file in Anchor's folder changes Anchor's memory.
  Suggested research topics (`/interests suggest`, and once a week on
  their own): the model gets the topics you saved (and the ones you
  retired), the texts of suggestions you dismissed, and, only while the
  lens is on, the lens notes' titles and summaries or the first 300
  characters of the text (a short note without a summary goes whole),
  capped overall by `INTEREST_SUGGEST_MAX_CHARS`. It gets no
  messages, memory, journal, notebook, summaries, check-ins, personal or
  knowledge notes or study cards. That call goes to OpenRouter only and
  searches nothing: nothing is sent to a search provider until you tap
  «добавить» or «добавить и искать» under a suggestion, which saves it as
  a topic you can remove (`/interests`); a topic is searched only when
  you run it or idle research reaches it. The suggestions, and what you
  did with each, are in `/export` and go with `/delete`; Claude Code
  can see only that they exist (ids, status, times), never their text.
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
