# Claude Code access: debug without reading the dialogs

Anchor's conversations, memory, journal and scene summaries live only
in the production Postgres. Claude Code may debug the bot but must not
read them. There are two layers:

1. **The database refuses.** Migration `9e4b2c7a1f05` creates the
   `debug` schema: views over operational columns only (ids, times,
   statuses, attempts, error codes, tokens, cost, text *lengths*). The
   `anchor_debug` role can read those views and no `public` table:
   `select content from public.message` is `permission denied`. This is
   the boundary that holds no matter what runs on the client side.
2. **Claude Code is fenced in.** `.claude/settings.json` denies the
   Railway MCP tools that reveal variables or run arbitrary operations
   (`list-variables`, `set-variables`, `railway-agent`,
   `create-tcp-proxy`, ...), `.env`, `railway run/connect/variables`,
   and the Telegram Bot API. `.claude/hooks/guard_private_data.py` runs
   before every Bash, file, WebFetch and Railway call and blocks the
   same things by pattern: reading the production secrets from the
   environment, environment dumps, literal non-local Postgres URLs,
   Railway hosts, `.env`. It is a guardrail, not a sandbox; layer 1 is
   what makes a bypass useless.

Still allowed: Railway `get-logs`, deployments, status, metrics,
traces (logs never carry message text, see `app/log.py`), local tests
and eval against a throwaway local database, and the `debug.*` views.
One content door is open on purpose, and only while you open it: lens
notes, through the `anchor_lens` role ("Lens notes" below).

**Phase 8: the Obsidian vault is off limits too**, except lens notes
through the `anchor_lens` role (L1, below). It holds the same
data as the database, as files. Claude never reads it and never
connects Obsidian tools to it: no Obsidian MCP server, no Local REST
API, no `ob` against the real vault. The vault service's logs, like the
bot's, carry no path, file name or note text, and they are fine to
read. vaultd's tests run on a temp directory and a fake `ob`. The
vault's credentials (`VAULT_API_TOKEN`, `OBSIDIAN_AUTH_TOKEN`,
`OBSIDIAN_E2EE_PASSWORD`) are blocked by the guard hook like the
others.

**The Claude connector is off limits to Claude Code.**
`anchor-claude-connector-plan.md` lets claude.ai read Anchor through a
custom connector, and a claude.ai account's connectors also appear in
Claude Code sessions and routines: as `mcp__Anchor__...` in cloud
sessions and `mcp__claude_ai_Anchor__...` in the CLI. The server cannot
tell a claude.ai chat from a Claude Code session, so this repo refuses
on its side: `.claude/settings.json` denies both server names, and the
guard hook, which now runs on every `mcp__*` tool, blocks any server
whose name starts with "anchor" and any tool named after one of
Anchor's read tools, so a renamed connector is still caught. A
malformed MCP call is blocked, not waved through. This protects
sessions in this repository only; the other layers are short `/claude`
windows and a Telegram notice on every read.

**Railway's `http` log stream is off limits too.** It records each
request's path, and Grok's capability URL is `/mcp/<token>`: the path
*is* the credential (and C2's authorize URL carries OAuth `state`). The
guard blocks `get-logs` whenever `types` includes `http`. The deploy,
build, network-flow and dns streams stay allowed, and so does
`http-requests`, which returns counts per status class, not paths.

## One-time setup

1. Deploy, so the migration runs (`alembic upgrade head` is part of the
   start command). It creates `anchor_debug` as `NOLOGIN`.
2. Enable login with a password of your choice. In the Railway
   Postgres service, open *Data → Query* (or `railway connect Postgres`
   from your own terminal, not from Claude):

   ```sql
   ALTER ROLE anchor_debug LOGIN PASSWORD '<a long random password>';
   ```

3. Build the URL from the Postgres service's **public** TCP proxy
   (host and port from `DATABASE_PUBLIC_URL`), replacing the user and
   password:

   ```
   postgresql://anchor_debug:<password>@<proxy-host>:<port>/railway
   ```

4. Put it in the Claude Code environment as `ANCHOR_DEBUG_DATABASE_URL`:
   the environment's variables/secrets on claude.ai/code, or your shell
   profile for a local CLI. **Never** give Claude the production
   database URL, the bot token or the OpenRouter key, and don't keep a
   production `.env` in a checkout Claude works in.

To revoke: `ALTER ROLE anchor_debug NOLOGIN;`.

## Queries Claude can run

```sh
psql "$ANCHOR_DEBUG_DATABASE_URL" -c "select status, count(*) from debug.job group by 1"
psql "$ANCHOR_DEBUG_DATABASE_URL" -c "select update_id, status, attempts, error from debug.telegram_update where status <> 'done' order by created_at desc limit 20"
psql "$ANCHOR_DEBUG_DATABASE_URL" -c "select local_date, category, sum(usd_cost) from debug.spend_ledger group by 1, 2 order by 1 desc limit 14"
psql "$ANCHOR_DEBUG_DATABASE_URL" -c "select * from debug.vault_status"
psql "$ANCHOR_DEBUG_DATABASE_URL" -c "select role, state, reason, count(*) from debug.vault_file group by 1, 2, 3"
```

The vault views (8a, migration `b8d24f6e0a17`) carry no path, no hash,
no hold payload and no chunk text. They have their own grant, because
`9e4b2c7a1f05`'s `GRANT ... ON ALL TABLES` covered only the views that
existed when it ran.

## Lens notes (L1)

`anchor-lens-plan.md` §11. The one exception to "the vault is off
limits": **lens notes**, the knowledge notes the user marked
`anchor: lens` or keeps under `lens_folders` (docs/vault-setup.md), may
be read by Claude Code, so that a session changing Echo's
self-improvement knows what Echo is told. Nothing else in the vault,
and no other class. Since L2 the door also returns, through
`lens.rounds(n)`, which notes each weekly review round picked and how
the round ended. Never the selector's rationale: it is model text
written from the analysis of the user's week, so it is derived from
the conversations and stays with the user (the Telegram card's
«почему эти заметки?»). Neither `lens.rounds(n)` nor
`debug.lens_round` carries it.

**What the role can do.** Migration `e4c7a2d9b1f3` creates
`anchor_lens` `NOLOGIN`, the same way `anchor_debug` is created, and a
`lens` schema of `SECURITY DEFINER` functions (two in L1; L2's
migration adds `lens.rounds(n)`). The role has `EXECUTE` on those and
nothing else: no `public` table, no `debug`
view, not even `lens_note` itself.

| Function | Returns |
|---|---|
| `lens.notes()` | `id, kind (person, concept), title, summary, body, chars, updated_at`, one row per lens note |
| `lens.graph()` | `src_title, dst_title, unresolved`: links between lens notes, and the targets of lens notes' links that name no note. Never a knowledge-only note, never a link to a note the bot may not see |
| `lens.rounds(n)` | `id, consumer, outcome, created_at, titles`: the last `n` (at most 50) rounds in which Echo chose lens notes (L2: the weekly review), newest first, with the current titles of the notes it chose (a note no longer in the lens is skipped) |

Each call inserts one `lens_read` row (function name, row count, time)
before it returns. That row lives in the caller's transaction, so a
client that rolls back (`begin; ... rollback`, a savepoint) takes the
row back with it. It cannot take back the row's id: each call takes it
first from `lens_read`'s sequence, and a sequence never rolls back. A
rolled-back read therefore leaves a gap in `lens_read.id`, and `/lens`
reports every gap as «Чтений без записи: N» (the daily digest too,
once a recorded read follows it). The count of recorded reads is exact
for a client that commits, which plain `psql -c` does; the gaps are how
a client that does not is still seen. One false gap is possible: a
Postgres crash can make a sequence skip ahead, and the `/lens` line
names that as the other cause.

The notes exist only while notes consent, `VAULT_KNOWLEDGE_ENABLED`
and `LENS_ENABLED` are all on. The sync pass deletes them when any one
is off: `lens.notes()` and `lens.graph()` then return nothing, and
`lens.rounds(n)` still lists past rounds (id, outcome, time) with empty
`titles`, since a round is review history, not a note. `/delete` erases
the rounds too.

### One-time setup

1. Deploy, so the migration runs. It creates `anchor_lens` as
   `NOLOGIN` and grants it what it needs. If the migrating user may not
   create roles, the migration skips the role with a NOTICE (and the
   grants with it). Then create it by hand, as a user that may:

   ```sql
   CREATE ROLE anchor_lens NOLOGIN;
   GRANT CONNECT ON DATABASE railway TO anchor_lens;
   GRANT USAGE ON SCHEMA lens TO anchor_lens;
   GRANT EXECUTE ON FUNCTION lens.notes(), lens.graph(), lens.rounds(int) TO anchor_lens;
   ```
2. Give it a password, and nothing more: the role stays `NOLOGIN`
   until you open it in step 5. In the Railway Postgres service, open *Data →
   Query* (or `railway connect Postgres` from your own terminal, not
   from Claude):

   ```sql
   ALTER ROLE anchor_lens PASSWORD '<a long random password, not the debug one>';
   ```

3. Build the URL exactly like the debug one, from the **public** TCP
   proxy (host and port from `DATABASE_PUBLIC_URL`):

   ```
   postgresql://anchor_lens:<password>@<proxy-host>:<port>/railway
   ```

4. Put it in the Claude Code environment as `ANCHOR_LENS_DATABASE_URL`,
   next to `ANCHOR_DEBUG_DATABASE_URL`. The guard hook allows it the way
   it allows the debug URL; it still blocks `DATABASE_URL`, the admin
   URL and literal Railway URLs.
5. When you want Claude Code to read the lens: `/lens code on` in
   Telegram (or `ALTER ROLE anchor_lens LOGIN;` by hand, if the bot may
   not alter roles). Until then the URL does not log in.

### Your switch: `/lens code on|off`

The password stays set; the switch is `LOGIN`.

- `/lens code on`: `ALTER ROLE anchor_lens LOGIN`.
- `/lens code off`: `ALTER ROLE anchor_lens NOLOGIN`, and
  `pg_terminate_backend` for every session the role already has open.
- `/lens` shows whether the lens is on, how many notes it holds (people
  and concepts), a warning above `LENS_CATALOG_MAX_NOTES`, whether
  Claude Code may log in, how many reads there were today, and any read
  whose record was rolled back (above), and «Последний разбор: <date>,
  <outcome>» for the weekly review's last lens round (L2).

If the bot's database user may not alter the role (no `CREATEROLE`), or
the role does not exist, the command says so and points here. Any other
database error (a lock timeout, a dropped connection) is reported as
such: nothing changed, try again. Then do
it by hand in *Data → Query*: `ALTER ROLE anchor_lens LOGIN;` or
`ALTER ROLE anchor_lens NOLOGIN;` followed by
`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'anchor_lens';`.

**The daily digest** (the one that reports `search_library` searches)
gains a line when there were reads: «Claude Code прочитал линзу: N
раз», counted over the 24 hours up to the digest's own time (21:00), so
a read later that evening is in the next day's digest rather than in
none. It is sent even with `CLAUDE_ACCESS_ENABLED` off, as long as
`LENS_ENABLED` is on, or the role may log in, or there was a read.

### Queries Claude can run

```sh
psql "$ANCHOR_LENS_DATABASE_URL" -c "select id, kind, title, chars from lens.notes()"
psql "$ANCHOR_LENS_DATABASE_URL" -c "select title, body from lens.notes() where id = 12"
psql "$ANCHOR_LENS_DATABASE_URL" -c "select * from lens.graph()"
psql "$ANCHOR_LENS_DATABASE_URL" -c "select id, outcome, created_at, titles from lens.rounds(10)"
psql "$ANCHOR_DEBUG_DATABASE_URL" -c "select * from debug.lens_round order by created_at desc limit 10"
psql "$ANCHOR_DEBUG_DATABASE_URL" -c "select * from debug.lens_read order by at desc limit 20"
```

Read only what the task needs: every call is a row the user sees
counted. Use plain `-c` (autocommit); a call inside a transaction you
roll back is still reported, as a read without a record. `debug.lens_note`, `debug.note_link`, `debug.lens_version` and
`debug.lens_read` carry ids, hashes, lengths, booleans and counts only:
no title, summary, body or link text. `debug.lens_round` (L2) has every
column but the selector's rationale. L2 also adds `debug.review_proposal`
(new in L2): ids, kind, status, times, `lens_round_id`, `lens_note_ids`
and `text_len`, never the proposal's text or reason. The titles are
read through `lens.rounds(n)`, a logged read like the other two. The
rationale is read by no role Claude Code has: only the user sees it,
in Telegram.

### Lens text stays in the session

The CLAUDE.md rule: lens text never leaves the session. Not in commits,
PR text, code comments, test fixtures, eval cases, logs or artifacts.
Paraphrase the idea from public knowledge ("Ashby's requisite
variety") and cite the note by its `lens.notes()` id. Tests and eval
use synthetic lens notes, as they use synthetic dialogs.

## Adding a table

A new table gets no view by default. If it should be debuggable, add a
view in a new migration with an **explicit** column list (never `*`)
and its own `GRANT SELECT ON debug.<view> TO anchor_debug`, and classify its text columns in
`CONTENT_COLUMNS` in `tests/test_debug_views.py`.
