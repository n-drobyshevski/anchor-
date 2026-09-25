# Claude access: opening your data to claude.ai, on purpose

Anchor can give Claude (claude.ai on the web, desktop or phone) read-only access to your data. It happens only when you ask for it, only to what you pick, and only for a limited time. It uses claude.ai's custom connector (Settings → Connectors → Add custom connector), which speaks MCP and signs in with OAuth. The specification is `anchor-claude-connector-plan.md`; the shapes it relies on were measured in a dry run (`docs/claude-connector-dry-run.md`).

There are two things, and you control both:

| | What it is | How long | Who approves it |
|---|---|---|---|
| **Connection** | claude.ai's one OAuth login to Anchor | 30 days at most, however often it refreshes | you, by typing `/claude connect <code>` in Telegram |
| **Window** | what the connection may read right now | 1 h or 24 h | you, with `/claude` |

**A connection with no open window reads nothing.** Claude can list the tools, but every call answers «Доступ закрыт. Открой его в Telegram: /claude».

## Turning it on

1. Set `CLAUDE_ACCESS_ENABLED=true` in the Railway service variables and redeploy.
   - It needs webhook mode and an `https://` `PUBLIC_URL`. The bot refuses to start otherwise.
   - Optional: `CLAUDE_WINDOW_MAX_HOURS` (default and maximum 24) and `CLAUDE_MAX_CALLS_PER_MINUTE` (default 30).
2. With the flag off, `/mcp/claude`, `/oauth/*` and the `/.well-known/...` routes do not exist, and `/claude` refuses.
3. **Turning the flag off revokes every connection at the next start.** Turning it back on never revives an old token.

## Connecting (about once a month)

1. In claude.ai, go to Settings → Connectors → **Add custom connector**.
   - Name: `Anchor`. The name decides how the tools appear in Claude Code (`mcp__<name>__…`), and `.claude/settings.json` denies `Anchor` (and `anc`, the name used first) by name. The guard hook still catches any other name by the tools' own names, but a known name gets both layers.
   - URL: exactly `https://<PUBLIC_URL>/mcp/claude`, with no port and no trailing slash.
   - Leave the OAuth Client ID and Secret fields empty.
2. Press **Connect**. A page from your Anchor opens and shows:
   > Открой Telegram и отправь боту: **/claude connect K7QX4M**
3. Type that command into Telegram (the code is on the page, never in Telegram). The bot answers «Подтверждено…». A few seconds later the page sends you back to claude.ai, and the connector shows as connected.
4. **If you did not start this yourself, close the page and type nothing.** Nobody but you can approve a request.
   - A wrong code answers «Код не найден или устарел.»
   - Five wrong codes in an hour lock `/claude connect` for an hour.
5. Connecting again replaces the old connection. Its tokens and windows close.

## Reading: `/claude`

1. **Send `/claude`.** It shows the connection, e.g. «Подключение #3 от 25.09, до 25.10», and a picker with everything off:
   - **Память**: active memories.
   - **Журнал**: journal entries and check-ins.
   - **Диалоги**: your messages and the bot's, plus scene summaries, going back 7 / 30 / 90 days. Never out-of-character (`/out`) messages, welfare exchanges or bot plumbing.
   - **Состояние**: focus, main action, streak, intensity, and the last 7 days of spend.

   Pick a lifetime (1 h / 24 h) and press **Открыть**. A new window closes the previous one.
2. **Ask Claude.** On the first read, and then at most every 10 minutes, the bot tells you what was read, e.g. «Claude (подключение #3) прочитал: журнал (14)».
3. **Close access:**
   - `/revoke` closes every window, and every Grok grant too;
   - `/claude disconnect` ends the connection itself;
   - `/delete` wipes everything, connections included.

## What leaves, and what doesn't

- **Anything Claude reads goes to Anthropic** and stays in that claude.ai conversation, under your claude.ai privacy settings (including whether chats may be used to improve models). **Closing a window stops further reads. It cannot recall what was already read.**
- **Text Claude reads can carry instructions.** If other connectors that can *write* (Drive, a planner, email) are enabled in the same chat, read Anchor in a chat without them.
- **Claude can only read.** None of the tools write, and none reach the queue, raw Telegram updates, pending memories, research data or vault notes.
- **Stored, and never exported:** only hashes of codes and tokens, plus ids and timestamps.
  - Pending requests live only in memory, for up to 10 minutes.
  - Logs carry ids, routes and outcomes. Never a token, code, `state`, confirmation code, cookie or client id.
  - aiohttp's access log is off.

## Claude Code can reach it too

**Claude Code can see your claude.ai connectors.** They show up in its cloud sessions and routines, as `mcp__<connector name>__…` (or `mcp__claude_ai_<connector name>__…` in the command-line tool). This was confirmed on the first connection: a connector named `anc` appeared in a Claude Code session as `mcp__anc__get_memory` and its three siblings. While a window is open, any Claude on your account could read through it, including a Claude Code session. Anchor's server cannot tell a claude.ai chat from a Claude Code session. claude.ai offers no switch to keep one connector out of Claude Code (checked in the dry run).

What keeps this small:
- **Windows are short and single.** 1 h or 24 h, one at a time. Most of the time the connector yields nothing.
- **Every read shows up in Telegram.** A read notice when you were not chatting with Claude is your cue to `/revoke`.
- **This repository refuses the tools.** `.claude/settings.json` and the guard hook block any Anchor connector tool, under any name. That protects Claude Code sessions *in this repository* only.

## For developers

- **Authorization server:** `app/web/oauth.py` (HTTP) and `app/web/oauth_store.py`, the only writer of `oauth_*`.
  - The pinned constants live there: `CLIENT_ID` (claude.ai's CIMD URL), `REDIRECT_URI`, the lifetimes and the caps.
  - CIMD only: no registration endpoint, and client metadata is never fetched.
- **Endpoint:** `app/web/mcp_claude.py`. It checks the bearer token and the window, then hands off to `app/web/mcp_core.py`, the server shared with Grok.
- **Windows:** `access_grant` rows with `client='claude'` and a `connection_id` (`app/core/grants.py`).
- **Telegram:** `app/tg/claude.py`, with callback data `cl:<action>:<mask>:<period>:<ttl>:<epoch>`. `/revoke` is `app/tg/access.py`.
- **Tables:** `oauth_connection`, `oauth_request`, `oauth_token`, created by migration `e6c1a9d3b527`.
  - Retention: `oauth_store.sweep`, run by the daily retention job.
  - Debug views: `debug.oauth_connection` and `debug.oauth_request` (ids, statuses and times only).
- **Tests:** `tests/test_claude_oauth.py`, `tests/test_claude_access.py`, `tests/test_claude_privacy.py`, `tests/test_claude_guard.py`.
