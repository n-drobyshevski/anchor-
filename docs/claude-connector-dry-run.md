# Claude connector: the dry run

**Done on 2026-09-25.** The results are below and pinned in
`docs/decisions.md` ("C2 — the dry run's answers"). The probe is off.

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

## Results (2026-09-25)

There were two runs: `both` at 19:58 UTC and `cimd` at 20:35 UTC. They matched request for request. The `dcr` run was skipped, because CIMD works on its own and DCR will never be offered.

| # | Request | What it showed |
|---|---|---|
| 1 | `POST /mcp/claude` from claude.ai's servers, with `mcp-protocol-version` and no `Authorization` | Discovery starts from the 401 challenge. |
| 2 | `GET /.well-known/oauth-protected-resource/mcp/claude` | The path-suffixed RFC 9728 URL named in `resource_metadata`. The root copy was never requested. |
| 3 | `GET /.well-known/oauth-authorization-server` | Root RFC 8414. No OpenID discovery, and no other `/.well-known` path. |
| — | no `POST /oauth/register` | **CIMD, in both runs,** including when DCR was also offered. |
| 4 | `GET /oauth/authorize` in the user's browser | Detailed below. |

What the authorize request (row 4) carried:
- `client_id` = `https://claude.ai/oauth/mcp-oauth-client-metadata`;
- `redirect_uri` = `https://claude.ai/api/mcp/auth_callback`;
- `resource` exactly the canonical URI;
- `code_challenge_method=S256`, `response_type=code`, `scope=anchor.read`;
- `state` present;
- no other parameters.

Other findings:
- **Authorize can arrive several times in a row.** In the `cimd` run it came four more times within three minutes, from two browser profiles: reloads, or Connect pressed again.
- **No per-surface switch (§11.6).** claude.ai's connector settings offered nothing to keep the connector out of Claude Code sessions or routines.
- **The first attempt failed in claude.ai's form.** The form rejected the URL before any request was made. The exact URL `https://<PUBLIC_URL>/mcp/claude`, with no port and no trailing characters, worked.

