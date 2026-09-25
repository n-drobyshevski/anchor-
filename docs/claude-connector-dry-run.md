# Claude connector: the dry run

`anchor-claude-connector-plan.md` section 11 asks what claude.ai
actually sends before C2 writes an authorization server: which client
registration it uses, its `client_id`, its callback, whether and how it
sends `resource`. `app/web/oauth_probe.py` answers that. It is an OAuth
front that looks real to claude.ai, grants nothing, and logs only
shapes.

## What the probe does and does not do

- It is off unless `CLAUDE_OAUTH_PROBE` is `both`, `cimd` or `dcr`.
  Off, none of its routes exist.
- It serves the discovery documents, a 401 on `/mcp/claude`, a
  registration answer (one fixed public `client_id`, nothing stored),
  and an authorize page that says «Проверка подключения: запрос
  получен. Подключение пока не работает.» **It never redirects back,
  so claude.ai never gets a code, a token or a connection.** Connecting
  is expected to fail or hang at that page.
- It writes nothing to the database and never messages you in Telegram.
- Each request logs one line, `event=oauth_probe`, with:
  - the route;
  - the names of parameters and headers;
  - yes/no comparisons: is the callback `https://claude.ai/api/mcp/auth_callback`, and is `resource` exact, differently cased, slashed or other;
  - the PKCE method;
  - the grant types;
  - the `client_id`'s host, and its path only when the host is claude.ai or claude.com.
  
  Never `state`, a challenge, a code, a token, a cookie or any header value. A `/.well-known/...` path the probe does not serve is logged by its path, so we see what else claude.ai looks for.

## Checklist (you)

Each run: set the variable in Railway → the bot service → Variables,
let it redeploy, then do the claude.ai part.

1. [ ] `CLAUDE_OAUTH_PROBE=both`.
2. [ ] claude.ai → Settings → Connectors → **Add custom connector**. Name `Anchor`, URL `https://<PUBLIC_URL>/mcp/claude`. Press **Connect**.
3. [ ] Note what claude.ai shows: which page opened, any error text. A page saying «Проверка подключения…» is the expected end. Close it.
4. [ ] Remove the connector in claude.ai.
5. [ ] Repeat steps 2–4 with `CLAUDE_OAUTH_PROBE=cimd`, then with `CLAUDE_OAUTH_PROBE=dcr`.
6. [ ] Look for a per-surface switch (plan §11.6): anything in claude.ai's connector settings that keeps a connector out of Claude Code sessions or routines. Write down its exact wording, or "none".
7. [ ] If a Claude Code session shows Anchor tools while the connector exists, note only their names (for example `mcp__Anchor__...`). Do not use them. There will be none while the probe answers 401.
8. [ ] Set `CLAUDE_OAUTH_PROBE=off`.
9. [ ] Tell me the rough times of each run. I read the bot's deploy logs, filtered to `oauth_probe`, which only I need; or you paste those lines to me.

## What it cannot answer

Plan §11.4 (does claude.ai present one refresh token twice within
seconds) and §11.5 (what claude.ai does with `isError: true`, and with
a 401 mid-conversation) need a server that issues tokens. They move to
C2's manual check, run with `CLAUDE_ACCESS_ENABLED=true` before it
stays on.

## Afterwards

The findings go into `docs/decisions.md` as pinned constants:
- the registration method;
- the CIMD `client_id` URL or the DCR request shape;
- the callback;
- the `resource` form.

C2's authorization server (`app/web/oauth.py`) replaces the probe, and the `CLAUDE_OAUTH_PROBE` setting goes away.
