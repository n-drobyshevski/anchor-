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

**Phase 8: the Obsidian vault is off limits too.** It holds the same
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

## Adding a table

A new table gets no view by default. If it should be debuggable, add a
view in a new migration with an **explicit** column list (never `*`)
and its own `GRANT SELECT ON debug.<view> TO anchor_debug`, and classify its text columns in
`CONTENT_COLUMNS` in `tests/test_debug_views.py`.
