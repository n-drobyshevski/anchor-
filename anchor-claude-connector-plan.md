# Anchor — Claude connector plan

Version: 2026-09-25 (rev. 2, after an independent security review) · Scope: read access to Anchor from **claude.ai** (web, desktop, mobile) through a custom connector, alongside the existing Grok access.
Parent docs: `docs/grok-access.md`, `app/web/mcp.py`, `app/core/grants.py`, `anchor-phase8e-plan.md` §7. For this feature, **this file wins**. Every earlier invariant stays in force.

Milestones are **C1–C3**. C1 and C2 can ship now; C3 depends on Phase 8d (notes).

---

## 0. Goal

In a claude.ai chat you can say "look at my journal for the last two weeks and tell me what pattern you see", and Claude reads it from Anchor. Four conditions must all hold:

- the connection was approved **by you, typing a code into Telegram**;
- you have opened a **time-boxed window** with `/claude`;
- the read is **for the scopes** that window allows;
- **every read is announced** in Telegram, as Grok's are today.

The connector is **read-only**. Nothing Claude does through it can change Anchor.

---

## 1. Why this is not "Grok, but for Claude"

Grok uses a **capability URL**: the token is in the path, and you paste a new link for every grant. That does not fit claude.ai, for three reasons.

1. **claude.ai connectors are built around OAuth.**
   - Discovery goes through Protected Resource Metadata. Sign-in uses the authorization-code flow with PKCE.
   - Clients register through Client ID Metadata Documents (CIMD) or Dynamic Client Registration (DCR). The callback is `https://claude.ai/api/mcp/auth_callback`.
   - Connectors with no authentication work for individual accounts. But the organisation-managed flow tries OAuth discovery regardless, and a no-auth connector is not the supported path for private data.
2. **A connector URL is a long-lived account setting.** A token in that URL would sit in claude.ai's settings indefinitely.
3. **claude.ai connectors also reach Claude Code.** Your account's connectors are available in Claude Code cloud sessions and routines. This very session has your Railway, Supabase and Google Drive connectors. So an Anchor connector puts your conversations one tool call away from Claude Code, which `CLAUDE.md` and `docs/claude-access.md` exist to prevent. No server can reliably tell which Claude surface made a request. The design therefore makes that exposure **short, visible and scoped**, rather than pretending to rule it out (§6.3).

**So the design separates two things:**

| | What it is | Lifetime | Who approves it |
|---|---|---|---|
| **Connection** | One OAuth client and its tokens. **At most one at a time.** | 30 days, absolute: refresh cannot extend it | you, by **typing** `/claude connect <code>` in Telegram |
| **Window** | Which scopes are readable right now, for that connection | 1 h or 24 h | you, with `/claude` |

A connected Claude with no open window can list the tools, but every call returns «Доступ закрыт».

---

## 2. Out of scope (do not build)

- **Write tools of any kind.** Proposing a memory or a note is a later decision (§12).
- **Personal vault notes.** Phase 8e §10 says personal note text never reaches a grant, and this plan does not amend that. Only knowledge notes become readable, in C3.
- **Changes to Grok access.** Only shared code is refactored; §11.7 flags one existing Grok issue for you to decide.
- **The Claude API's MCP connector.**
- **Submitting to the connector directory.**
- **A web login.** Approval happens only by typing a command into Telegram.

---

## 3. New config

```
CLAUDE_ACCESS_ENABLED=false           # master switch; off = /mcp/claude, /oauth/* and the well-known routes answer aiohttp's own 404
CLAUDE_WINDOW_MAX_HOURS=24            # /claude offers 1 h and 24 h
CLAUDE_MAX_CALLS_PER_MINUTE=30        # its own limiter, separate from Grok's
```

It needs webhook mode (`MODE=webhook` + `PUBLIC_URL`), like Grok. **Turning the flag off revokes every connection**, so turning it back on cannot bring old tokens back to life.

**Constants, not settings** (a deploy must not be able to loosen them):

| Constant | Value |
|---|---|
| Access-token lifetime | 60 min |
| Connection lifetime | 30 days, absolute |
| Refresh token | rotated on every use; its lifetime is capped by the connection's |
| Authorization code | 60 s, single use |
| Pending authorize request | 10 min |
| Pending requests | at most 5 per IP and 20 in total, held in memory (§5.2) |
| Allowed `redirect_uri` | exactly `https://claude.ai/api/mcp/auth_callback` |

**No new dependency.** The authorization server is small and single-user, has one grant type, and is hand-written and exhaustively tested. If you would rather use a library, [Authlib](https://pypi.org/project/Authlib/) ([GitHub](https://github.com/authlib/authlib)) is the one to consider (§12).

---

## 4. Endpoints

Let `<R>` = `https://<PUBLIC_URL>/mcp/claude`, the canonical resource URI, and `<ISS>` = `https://<PUBLIC_URL>`. Both are built once with `rstrip('/')`, and `issuer` equals the `authorization_servers` entry **byte for byte**.

| Route | Purpose |
|---|---|
| `POST /mcp/claude` | The MCP endpoint: stateless JSON-RPC (`initialize`, `ping`, `tools/list`, `tools/call`). Requires `Authorization: Bearer`. A missing, invalid, expired or revoked token gets **401** with `WWW-Authenticate: Bearer resource_metadata="<ISS>/.well-known/oauth-protected-resource/mcp/claude", scope="anchor.read"`. |
| `GET /.well-known/oauth-protected-resource/mcp/claude` | RFC 9728: `resource: <R>`, `authorization_servers: [<ISS>]`, `scopes_supported: ["anchor.read"]`, `bearer_methods_supported: ["header"]`. The same document is also served at the root `/.well-known/oauth-protected-resource`. |
| `GET /.well-known/oauth-authorization-server` | RFC 8414: `issuer`, `authorization_endpoint`, `token_endpoint`, `revocation_endpoint`, `response_types_supported: ["code"]`, `grant_types_supported: ["authorization_code","refresh_token"]`, `code_challenge_methods_supported: ["S256"]`, `token_endpoint_auth_methods_supported: ["none"]`, `revocation_endpoint_auth_methods_supported: ["none"]`, `authorization_response_iss_parameter_supported: true` (RFC 9207), `scopes_supported: ["anchor.read"]`. **Exactly one** of `client_id_metadata_document_supported: true` or `registration_endpoint`, per §5.1. |
| `GET /oauth/authorize` | Validates the request and serves a waiting page that shows the code (§5.2). |
| `GET /oauth/authorize/status?h=<handle>` | Polled by the waiting page. Answers only with the browser-binding cookie (§5.2). |
| `POST /oauth/token` | `authorization_code` and `refresh_token` (§5.3). |
| `POST /oauth/revoke` | RFC 7009 (§5.3). |
| `POST /oauth/register` | Only if §5.1 settles on DCR. |

**On every `/oauth/*` response:**
- `Cache-Control: no-store`;
- `Referrer-Policy: no-referrer`;
- a `Content-Security-Policy` with `frame-ancestors 'none'`.

The aiohttp access log stays off for `/mcp/*` and `/oauth/*`, as it already is for `/mcp/{token}`. **Tokens never appear in a URL.**

---

## 5. Connecting (at most once a month)

### 5.1 Client registration: verify first, then pick one

Before any OAuth code is written, record what claude.ai actually sends (§11). Log only hosts and field names, never values.

- **CIMD** (the MCP spec's `SHOULD`):
  - `client_id` is an HTTPS URL. Accept **only** a `client_id` on an exact allowlist of claude.ai metadata URLs, recorded as constants after the dry run.
  - **Never fetch arbitrary client metadata** (SSRF). Pin the allowlisted document's `redirect_uris` in code.
  - If claude.ai changes its URL, connecting fails closed, and the waiting page says «Клиент не распознан».
- **DCR** (the fallback): `POST /oauth/register` accepts a registration only when `redirect_uris` is exactly `[https://claude.ai/api/mcp/auth_callback]` and the grant types are within the two above.
  - Whatever `token_endpoint_auth_method` is requested, it answers `none` (RFC 7591 allows that).
  - A registration is capped per IP, and deleted after 1 h unless a connection uses it. **There is no eviction by count**, so an anonymous registrant cannot push out yours.

**Advertise exactly one of the two,** so claude.ai has no choice to get wrong.

### 5.2 Approval: the code travels from your screen to Telegram

**Unauthenticated web traffic never makes Anchor send you a Telegram message.** You carry the code from the browser into Telegram yourself.

1. **claude.ai sends your browser to `/oauth/authorize`.**
   - Validate everything first:
     - `response_type=code`;
     - an allowed `client_id` and `redirect_uri`;
     - `code_challenge` with `code_challenge_method=S256`;
     - `resource` equal to `<R>` after normalising the case of scheme and host and stripping a trailing slash;
     - `scope` a subset of `{anchor.read}` (empty means `anchor.read`);
     - `state` present.
   - **A failure shows a plain error page and never redirects** (OAuth 2.1's rule for untrusted redirects).
2. **Create a pending request in memory,** not in the database: it is cheap to create, and nothing is written for a stranger. It gets:
   - a random 128-bit **handle**;
   - a 6-character **confirmation code** (uppercase, no 0/O/1/I);
   - a random **browser-binding secret**, set as a `__Host-anchor_oauth` cookie (`Secure; HttpOnly; SameSite=Lax; Path=/`). Only its hash is kept.

   Caps: 5 pending requests per IP, 20 in total, 10 minutes each. A cap refusal page reveals nothing about existing connections.
3. **The waiting page shows** (in Russian):
   > Открой Telegram и отправь боту: **/claude connect K7QX4M**
   > Если ты не подключал Claude сам — просто закрой эту страницу.
4. **You type `/claude connect K7QX4M` in Telegram.** The code matches only an unexpired pending request. A wrong code answers «Код не найден или устарел.» and counts toward a limit of 5 failures per hour; after that, `/claude connect` refuses for an hour.
   - **A match** writes the `oauth_request` row (approved), mints a 60-second authorization code bound to this `client_id`, `redirect_uri`, `code_challenge` and `resource` (only its hash is stored), and replies:
     > Подключено. Старое подключение (если было) закрыто. Читать Claude сможет только в окне: /claude
5. **The waiting page's next poll** of `/oauth/authorize/status?h=…` must carry the matching cookie. It then redirects to `redirect_uri?code=…&state=…&iss=<ISS>`. **A poll without the cookie gets the same answer as an unknown handle.** A lucky guess of someone else's handle therefore yields nothing: not the code, not `state`, not even whether the handle exists.
6. **claude.ai exchanges the code** at `/oauth/token` (§5.3). The connection now exists, and **it replaces any previous connection.** The old one's tokens are revoked, and its windows close.

**Why this order.** A stranger can start an authorize request with claude.ai's own client and their own claude.ai account. With a Telegram button, one careless tap would hand them a token. With a typed code, the only codes you ever type are the ones on your own screen.

### 5.3 Token and revocation endpoints

**Authorization-code exchange:**
- Redeem in **one atomic statement**: `UPDATE … SET status='redeemed' WHERE code_sha256=… AND status='approved' AND code_expires_at > now() AND client_id=… RETURNING …`.
- Then check the PKCE verifier (S256) and `resource`, normalised as in §5.2.
- **A replayed code** gets `invalid_grant`, and **revokes every token issued from it** (OAuth 2.1 §4.1.3).

**Response:** `token_type: "Bearer"`, `expires_in`, `access_token` and `refresh_token`. Tokens are opaque, 256-bit random, and bound to `<R>`; only their sha256 is stored.

**Refresh:**
- Rotation is atomic, and `resource` may be absent at refresh.
- **Reuse of a replaced refresh token revokes the connection**, the standard theft signal. The one exception is the 30 seconds after a rotation: a second presentation of the just-replaced token then gets a plain `invalid_grant` and revokes nothing. That tolerates claude.ai retrying a request whose response was lost. The trade-off is written down, not hidden.
- A refresh token never outlives its connection's absolute expiry.

**Revocation (RFC 7009):**
- Requires `client_id`. The token must have been issued to that client; otherwise, and for unknown tokens, answer 200 with no effect.
- Revoking a refresh token also revokes its access tokens.

**`/mcp/claude` accepts a token only if all five hold:**
- it hashes to an unexpired, unrevoked access token;
- its audience is `<R>`;
- its connection is active and within its absolute lifetime;
- the connection is the current one;
- `CLAUDE_ACCESS_ENABLED` is true.

**Ending a connection:**
- `/claude disconnect` revokes the connection;
- `/revoke` closes windows (and Grok grants);
- `/delete` truncates every OAuth table (§7).

In each case claude.ai's next call gets a 401, and it asks to reconnect.

---

## 6. Reading (every time)

### 6.1 `/claude`

**Status first.** `/claude` starts with the connection's status: «Подключение #3 от 25.09, до 25.10» or «Нет подключения», plus the setup steps (§6.4).

**The picker.** With a connection, it offers the same picker as `/grok`:
- **Scopes**, all off by default: Память (memory), Журнал (journal), Диалоги (dialogs, with a look-back of 7 / 30 / 90 days), Состояние (state), and Библиотека (knowledge notes, C3 only).
- **Lifetime:** 1 h / 24 h.
- **Buttons:** [Открыть] [Отмена].

**Windows.**
- A window is an `access_grant` row with `client='claude'`, no token, and the **connection id** it belongs to.
- **Opening a window closes any open Claude window**, so exactly one exists at a time.
- The picker's callbacks follow `/grok`'s conventions: the issued-at timestamp in the data, staleness through `is_fresh`, and the same `is_web_sink` guard as `grok_decision`.

**Grok is unaffected.** `list_active`, the texts that mention active grants, and `/revoke`'s reply become client-aware: «Доступ закрыт: Grok (1), Claude (1)».

### 6.2 Tools

**Shared with Grok.** The four existing tools (`get_memory`, `get_journal`, `get_dialogs`, `get_state`) and their content rules come from one shared core (§8, C1). What an outside assistant may see is still decided in `app/core/grants.py` and nowhere else, so welfare exchanges, out-of-character messages and plumbing stay excluded exactly as for Grok.

**`tools/list`** returns every tool the token's scope allows. Clients cache tool lists, so the list does not follow the window.

**`tools/call` outside an open window**, or for a scope the window does not include, returns a tool result with `isError: true`:
> Доступ закрыт. Открой его в Telegram: /claude

- It is deliberately not a 403: `insufficient_scope` would send claude.ai into step-up re-authorisation, which here would mean a new connection on every call.
- **A 401 stays reserved for token problems,** as the spec requires.
- The shared core takes the refusal style as a parameter. Grok keeps its current JSON-RPC `-32602` for an out-of-scope tool.

**Notices.** Every read is counted, and Telegram gets «Claude (подключение #3) прочитал: журнал (14)» on the first read of a window and then at most every 10 minutes. Same rule and same function as Grok.

**Instructions.** `SERVER_INSTRUCTIONS` keeps Grok's wording and adds: read only what the user's question needs.

### 6.3 Claude Code: the problem this plan cannot fully solve

While a window is open, any Claude surface on your account can use the connector, including a Claude Code session or routine. Four layers keep that small and visible:

1. **Short, single windows.** The default is 1 h and the maximum 24 h, and at most one window is open. Most of the time the connector yields nothing.
2. **Every read shows up in Telegram.** An unexpected «Claude прочитал…» while you are not chatting is your signal to `/revoke`.
3. **This repo refuses the tools.** claude.ai connectors appear as `mcp__Anchor__…` in cloud sessions (spaces become `_`) and as `mcp__claude_ai_Anchor__…` in the local CLI. So:
   - `.claude/settings.json` denies both `mcp__Anchor` and `mcp__claude_ai_Anchor`;
   - `guard_private_data.py`'s matcher widens from `mcp__Railway__.*` to `mcp__.*`. It blocks any tool matching `(?i)^mcp__(?:claude_ai_)?anchor\w*?__`, and also any `mcp__*` tool whose final segment is one of Anchor's tool names, so a renamed connector is still caught;
   - the hook's current fall-through on malformed input becomes a **block** for `mcp__*` tools;
   - `CLAUDE.md` gains the rule in words.

   These protect Claude Code sessions **in this repo** only.
4. **Your account settings.** If claude.ai lets you keep a connector out of Claude Code sessions and routines, use that (§11 checks).

**The server cannot tell claude.ai chat from Claude Code.** Both use the token claude.ai holds, through Anthropic's side, and nothing in this plan claims otherwise.

### 6.4 What leaves

- **Anything Claude reads goes to Anthropic** and stays in that conversation, subject to your claude.ai privacy settings, including whether chats may be used to improve models. **Closing a window stops further reads. It cannot recall what was already read.**
- **Text Claude reads can carry instructions** into a chat where other connectors are enabled, some of which can write (Drive, a planner). `docs/claude-connector.md` says so, and recommends reading Anchor in a chat without write-capable connectors.
- The picker text, `/privacy` and `docs/privacy.md` each gain one sentence.

---

## 7. Data model

```sql
access_grant                              -- existing
  + client         text not null default 'grok' check (client in ('grok','claude'))
  + connection_id  bigint references oauth_connection(id) on delete cascade
  ~ token_sha256   nullable; unique stays (NULLs don't conflict)
  + check ((client = 'grok') = (token_sha256 is not null))
  + check ((client = 'claude') = (connection_id is not null))
  ~ ck_access_grant_scopes gains 'notes_knowledge' in C3, and never 'notes_personal' (Phase 8e §10)

oauth_connection (
  id            bigserial primary key,
  client_id     text not null,
  created_at    timestamptz not null default now(),
  expires_at    timestamptz not null,       -- absolute; created_at + 30 days
  last_used_at  timestamptz,
  revoked_at    timestamptz,
  check (expires_at > created_at)
);
-- at most one active: unique index on ((true)) where revoked_at is null

oauth_request (                            -- approved requests only; pending ones live in memory (§5.2)
  id              bigserial primary key,
  client_id       text not null,
  redirect_uri    text not null,
  resource        text not null,
  scope           text not null,
  code_challenge  text not null,
  code_sha256     text not null unique,
  code_expires_at timestamptz not null,
  status          text not null check (status in ('approved','redeemed','expired')),
  connection_id   bigint references oauth_connection(id) on delete cascade,  -- set on redemption
  created_at      timestamptz not null default now()
);

oauth_token (
  id            bigserial primary key,
  connection_id bigint not null references oauth_connection(id) on delete cascade,
  request_id    bigint references oauth_request(id) on delete cascade,        -- for replay revocation
  kind          text not null check (kind in ('access','refresh')),
  token_sha256  text not null unique,
  audience      text not null,
  expires_at    timestamptz not null,
  revoked_at    timestamptz,
  replaced_by   bigint references oauth_token(id),
  replaced_at   timestamptz              -- the 30 s retry grace (§5.3)
);

oauth_client (                            -- DCR only (§5.1); absent if CIMD is chosen
  client_id     text primary key,
  redirect_uri  text not null check (redirect_uri = 'https://claude.ai/api/mcp/auth_callback'),
  created_at    timestamptz not null default now()
);
```

**Lifecycle:**
- **`/delete`:** every `oauth_*` table joins `PURGED_TABLES`, child-first. `/delete` also clears the in-memory pending requests.
- **`/export`:** every `oauth_*` table joins `NOT_EXPORTED`, with the reason "credentials and approval plumbing; hashes only, no content". `access_grant` stays exported, now with `client` and `connection_id`.
- **Retention sweep** (`app/core/retention.py`):
  - requests older than 1 day go;
  - tokens past expiry go. **A replaced refresh token is kept until its own expiry,** because deleting it earlier would blind reuse detection;
  - revoked connections go after 30 days.

**Debug views:** `debug.oauth_connection` (id, timestamps) and `debug.oauth_request` (id, status, timestamps). **Never** `client_id`, codes, challenges or hashes. Each gets its own `GRANT` to `anchor_debug`.

---

## 8. Code layout

- **`app/web/mcp_core.py` (C1).** The JSON-RPC dispatcher, the tool specs, `_call_tool` and the per-key rate limiter move here from `app/web/mcp.py`. The refusal style is a parameter.
  - `mcp.py` keeps only Grok's capability-URL authentication.
  - **`tests/test_grok_access.py` passes unmodified.**
- **`app/web/mcp_claude.py` (C2):** bearer authentication, the 401 challenge, and window checks. It has its own limiter instance, keyed by connection id.
- **`app/web/oauth.py` (C2):** the metadata routes, authorize, the in-memory pending store, status, token, revoke and optional register. It is **the only writer of `oauth_*`**.
- **`app/core/grants.py`:** `create_grant(..., client=, connection_id=)`, `find_open_window(connection_id)` (at most one by construction), `list_active(client=)`, and `record_use` naming the client and connection. The content functions are unchanged.
- **`app/tg/claude.py`:**
  - `/claude`, `/claude connect <code>` and `/claude disconnect`;
  - `/revoke` moves to a shared module for both clients;
  - every Claude callback has the `is_web_sink` guard.

**Isolation (AST test, the same pattern as the others).**
- `app/web/oauth.py` and `app/web/mcp_claude.py` import no LLM provider, no `update_state`, no memory writer, and no outbound or proposal module.
- They reach content only through `app/core/grants.py`'s read functions.
- No `app/web/` module imports `app.vault.notes_personal`.

---

## 9. Milestones

| Milestone | Mode | Contents |
|---|---|---|
| **C1. Shared MCP core** | behaviour unchanged | The extraction, and `access_grant.client` / `connection_id` with their migration. Grok's tests pass untouched. |
| **C2. OAuth + windows** | `CLAUDE_ACCESS_ENABLED=false` until the manual check | The §11 dry run and its decision in `docs/decisions.md`. Endpoints, typed-code approval, rotation, revocation, `/claude`, notices, the Claude Code guard, the retention rules, `docs/claude-connector.md`, and the `/privacy` line. |
| **C3. Library** | after Phase 8d | Scope `notes_knowledge` and a tool `search_library(query)` that returns at most 6 knowledge chunks as strings, via `app.vault.notes_knowledge.search`. Adds `app/web/mcp_core.py` to Phase 8e §8's allowlist for `notes_knowledge`, with a row in 8e §7's table citing this plan. |

**C2's manual check:**
1. Add the connector in claude.ai under the name `Anchor`, and type the code from the waiting page into Telegram.
2. Ask Claude to read the journal with no window open: it gets «Доступ закрыт».
3. Open a window with `/claude` for Журнал, 1 h.
4. Ask again: it works, and Telegram shows «Claude (подключение #…) прочитал: журнал (…)».
5. Run `/revoke`, and ask again: «Доступ закрыт».
6. Connect again: the old connection is gone.
7. Then flip `CLAUDE_ACCESS_ENABLED=true` permanently.

---

## 10. Tests (required)

**Metadata**
- RFC 8414 and RFC 9728 documents, including `S256`, `iss` support, and `issuer` equal to `authorization_servers[0]` byte for byte.
- The exact 401 challenge.

**Authorize**
- Each of these refuses with **no redirect and no database write**:
  - a wrong `redirect_uri` (a trailing slash, other case, `http`, an extra query);
  - an unknown `client_id`;
  - PKCE `plain`, or a missing challenge;
  - a wrong `resource`;
  - a missing `state`.
- `resource` with an uppercase host or a trailing slash is accepted.
- Pending caps apply per IP and in total.

**Approval**
- **No Telegram message is sent for any web request, ever.** Assert on the mock bot across the whole flow.
- A wrong code is refused, and 5 wrong codes lock `/claude connect` for an hour.
- A right code approves only its own request.
- A status poll without the cookie, or with another request's cookie, gets the unknown-handle answer, and so does a guessed handle.
- The redirect carries `code`, `state` and `iss`.

**Token**
- A single-use code; a replay gets `invalid_grant` **and** revokes that code's tokens.
- A wrong verifier, an expired code, a code presented by another `client_id`, and a wrong `resource` each get `invalid_grant`.
- Two concurrent redemptions: exactly one succeeds.

**Refresh**
- Rotation works.
- Reuse within 30 s is refused without revocation; reuse after 30 s revokes the connection.
- The connection's absolute expiry wins over any refresh.

**Revoke**
- An unknown token gets 200.
- Another client's token gets 200 with no effect.
- Revoking a refresh token revokes its access tokens.

**Connection**
- A second connection revokes the first and closes its windows.
- Turning the flag off revokes everything, and turning it on again does not revive anything.

**Windows**
- No window → `isError`.
- An out-of-window scope → `isError`.
- An expired window → `isError`.
- A new window closes the old one.
- `/revoke` closes Claude windows and Grok grants alike.
- A Grok token never works on `/mcp/claude`, and a Claude token never works on `/mcp/{token}`.
- The Grok and Claude limiters are independent.

**Content**
- The same data through both clients gives identical payloads.
- Welfare, out-of-character messages and plumbing are excluded.

**Privacy**
- Over a full connect-and-read flow, no log record contains a token, code, `state`, confirmation code, handle, cookie or `client_id` URL (caplog).
- The access log is off for `/mcp/*` and `/oauth/*`.
- The debug views expose no secret column.
- The security headers are present on every `/oauth/*` response.

**Delete and export**
- Coverage tests are extended.
- After `/delete`, the old access token gets 401, and the in-memory pending store is empty.

**Guard**
- These are blocked: `mcp__Anchor__get_journal`, `mcp__claude_ai_Anchor__get_journal`, `mcp__anchor_2__get_memory`, `mcp__Renamed__get_dialogs`.
- `mcp__Railway__get-logs` is still allowed.
- Malformed input for an `mcp__*` tool is blocked.

**Isolation**
- The §8 AST rules.
- In C3, the 8e rules with the new allowlist entry.

**Flag**
- With it off, every new route returns aiohttp's own 404, byte for byte.

---

## 11. Verify before coding, and report

1. **Registration:** which method claude.ai uses when both are offered, and when each is offered alone (the CIMD `client_id` value, or the DCR request body). Record hosts and field names, then pin them as constants.
2. **Callback:** `https://claude.ai/api/mcp/auth_callback` is still the callback.
3. **`resource`:** whether claude.ai sends it, and in which form.
4. **Retries:** whether claude.ai ever presents one refresh token twice within seconds. This tunes the 30 s grace.
5. **Errors mid-conversation:** what claude.ai does with a tool result where `isError: true` (it should show the text and not re-authenticate), and on a 401 mid-conversation (it should refresh).
6. **Claude Code exposure:** whether claude.ai offers a per-surface switch that keeps a connector out of Claude Code sessions and routines. Document it either way, and confirm the exact tool-name forms in a cloud session and in the CLI.
7. **An existing Grok issue:** Claude Code is allowed `mcp__Railway__http-requests`. Check, **from the tool's documentation, not by calling it on production**, whether it reports request paths. If it does, Grok's capability URL (`/mcp/<token>`), and this plan's `/oauth/authorize?…state=…`, are visible to Claude Code. Report it, and propose denying that tool in `.claude/settings.json`.

If any answer contradicts this plan, stop and propose the smallest change.

---

## 12. Decisions for you

1. **A hand-written authorization server or Authlib.** The plan assumes hand-written: the flow is one grant type and the tests are specified. Authlib is well-trodden, but it brings a large surface.
2. **Write access later?** A `propose_memory` tool, where Claude suggests and a Telegram button confirms, would fit "the model proposes, the code decides". It needs its own plan.
3. **Personal notes.** They stay unreachable through any connector (8e §10). Changing that means amending 8e first, deliberately.
