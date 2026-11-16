# Echo — the code window: Claude Code reads vault notes on request (plan)

Version: 2026-11-16 (rev. 1) · Scope: a **debug mode** the user opens from Telegram, for a limited time and optionally for a subset of folders, during which Claude Code may read vault notes over HTTPS. Read-only. Closed by default, and closed again by itself.
Parent docs: `anchor-phase8-plan.md` (vaultd, §5.4 API), `anchor-phase8e-plan.md` (classes), `anchor-lens-plan.md` §11 (the lens door), `anchor-claude-connector-plan.md` §6 (windows, read notices), `docs/claude-access.md`. For this feature, **this file wins**. Every other invariant stays in force.

Milestone: **CW1** (all of this file).

---

## 0. Goal

**Today.** Claude Code may read lens notes only, through the `anchor_lens` Postgres role. That door is raw TCP, which a Claude Code cloud session cannot open, and it never covers a note that is not lens.

**After this plan.** The user types `/vault code on` in Telegram, confirms a card, and for 1, 6 or 24 hours Claude Code can list and read the notes of the folders the user named (or of the whole vault), through an HTTPS route on the bot. Every read is counted, the first read of a window is announced in Telegram, and the daily digest says how much was read.

**Settled decisions (owner, 2026-11-16).**

1. **Ceiling.** A window can open any of the user's notes: `knowledge`, `lens`, `personal` and unclassified. Two things stay closed in every window: a note whose effective class is `never`, and everything under `Anchor/` (memory, journal, reports, settings: the bot's own folders, which hold the conversations).
2. **Transport.** An HTTPS route on the bot's web server, not a Postgres role.

## 1. What changes in the contract, and what does not

- **Superseded, inside an open window only:** CLAUDE.md's "never read the Obsidian vault"; 8e's "a note that is unclassified is invisible to the bot" (the bot process relays such a note's path and text, and stores neither); `app/vault/client.py`'s "the bot cannot know the names of notes it may not see" (the relay sees them in memory for one request).
- **Unchanged:**
  - `never` and `Anchor/` are refused by vaultd itself, whatever the bot asks.
  - The database, the bot's own connector, Railway's `http` log stream and every secret stay off limits to Claude Code. The guard hook keeps blocking them.
  - Nothing a window serves reaches a prompt, a model call, a table or a log line. The existing consumers (chat turn, review, idle kinds, connector) see exactly what they saw.
  - Note text read through a window never leaves the Claude Code session: the lens rule (CLAUDE.md) applies to it word for word.
  - vaultd keeps no public domain. Claude Code never talks to vaultd.

## 2. Out of scope (do not build)

- Writes of any kind. Search. Attachments or any file that is not `.md`.
- A window that opens itself, renews itself, or is opened from the web UI, a model's output or Claude Code.
- A per-window token, or a token in a URL path or query.
- Storing or logging a path, a title or note text on the bot or in vaultd's logs.
- Reusing the claude.ai connector, its OAuth connection or its tools.

---

## 3. vaultd

Dark unless `VAULT_CODE_ACCESS` is truthy on the vault service (default off; parsed in `config.py` like the other switches). `GET /v1/status` gains `"code_access": true|false`.

Two routes, both `POST` with a JSON body so that no path ever rides in a query string. Off, both are the bare 404.

### `POST /v1/code/tree`

Body `{"roots": [<rel folder>, ...]}`. An empty list means the whole vault. At most 16 roots.

Answer `{"files": [{"path", "class", "bytes", "mtime"}], "truncated": bool}`:

- every `.md` under the roots that §3.3 allows, sorted by path, at most `CODE_TREE_MAX` (5000) entries; `truncated` says the cap cut it;
- `class` is `personal`, `knowledge`, `lens` or `none` (unclassified);
- `mtime` is UTC ISO-8601, seconds.

A root that does not exist, is not a folder or is itself refused by §3.3 contributes nothing. It is not an error, so a root cannot be used to probe.

### `POST /v1/code/file`

Body `{"path": <rel>, "roots": [...]}`. Answer `{"path", "class", "sha256", "text"}`.

vaultd checks that the path lies under one of the roots (an empty list allows any), on top of the bot's own check. Every refusal is the one `{"error": "not_found"}` 404 the read routes already use: malformed-but-parseable paths outside the roots, a missing file, a folder, a symlink, a dot-folder, `Anchor/`, `never`, not `.md`, not UTF-8, larger than `CODE_FILE_MAX_BYTES` (2 MiB). A body that is not the expected JSON, or a path that `paths.parse_rel` calls malformed, is 400, as elsewhere.

### 3.3 What a code route may serve

One function, used by both routes, next to `classes.effective_class` (never a copy of its rule):

- `.md` only; no segment starting with a dot; reached without a symlink (paths.py's `O_NOFOLLOW` walk);
- never `Anchor` or anything under it, compared the way `ANCHOR_DIRS` is today, and case-insensitively, so `anchor/Memory` on a case-insensitive disk is closed too;
- effective class is not `never` (property or folder rule, stricter wins, as everywhere);
- **fail closed on the settings file:** when its state is `invalid` or `wrong_case`, both routes answer 409 `{"error": "settings_unusable"}`. In those states a `never_folders` rule the user wrote is not being applied, so nothing may be served. `missing` is fine: there are no folder rules, and a note's own `anchor: never` still closes it.

### Logs and tests

Logs carry the route template, the status, the latency and, for the tree, the entry count. Never a path, a root or text.

`vaultd/tests/test_code_access.py`: off is 404 for both routes; each refusal above (one test each, all the same 404 body); roots confinement including `..`-free siblings that share a prefix (`Notes` must not open `Notes2`); `never` by property and by folder rule; `Anchor/` in any case; unclassified and personal notes are served; `invalid` and `wrong_case` are 409; the tree cap; status reports the switch; a log capture proving no path is logged.

## 4. The bot

### 4.1 Settings (all dark by default; listed in the README status table)

- `CODE_ACCESS_ENABLED` (false). Needs webhook mode on https and `VAULT_MODE` other than `off`, checked at startup like `CLAUDE_ACCESS_ENABLED`.
- `CODE_ACCESS_TOKEN_SHA256`: lower-case hex sha256 of a token the user generated (`secrets.token_urlsafe(32)` or longer). Startup refuses when the feature is on and this is not 64 hex characters. The token itself never reaches the bot's environment.
- `CODE_FILES_PER_WINDOW` (2000, 1–20000): file reads one window may serve; above it the route answers 429 until the window closes.

### 4.2 Storage (one migration)

| Table | Columns | Written by |
|---|---|---|
| `code_window` | `id, status (pending, open, closed), roots jsonb, ttl_s, created_at, opened_at, expires_at, closed_at, closed_reason (expired, off, replaced, delete), notified_at` | `app/vault/code_access.py` |
| `code_read` | `id, window_id → code_window, at, route (tree, file), files, bytes` | the same module |

- No path, title or text in either. `roots` holds what the user typed into Telegram (already in `message`).
- At most one window is `open`. Opening one closes any other (`replaced`).
- A window past `expires_at` is treated as closed by every reader; the row is marked `closed`/`expired` lazily, by whoever notices.
- Both tables join `PURGED_TABLES` (`/delete`), and are named as left out in `tests/test_export.py`, like `lens_read`.
- Debug views for `anchor_debug`: `debug.code_window` without `roots` (with `n_roots`), and `debug.code_read`.

`app/vault/code_access.py` is the only module that names the two tables (pinned in `tests/test_vault_notes_isolation.py` or a sibling test). It stores counts and never receives text.

### 4.3 Telegram: `/vault code` (Telegram only, in `BLOCKED_COMMANDS` for the web sink)

- `/vault code`: whether the feature is on, whether vaultd's switch is on, the open window (until when, which roots, files read so far), or when the last one closed.
- `/vault code on [1h|6h|24h] [папка; папка; ...]`: default `1h`, default no roots (the whole vault). Roots are split on `;`, at most 8, each a well-formed vault-relative folder path, not `Anchor` and not under it (any case). The bot asks vaultd's tree for the count, then replies with a card: how many notes would open, the roots or «весь vault (кроме Anchor/ и never)», the lifetime, and buttons «Открыть» / «Отмена». The choice lives in a `pending` row; the callback carries only its id and issue time, and a button older than `CONFIRM_TTL` is stale. Nothing is open until «Открыть».
- `/vault code off`: closes the open window now.
- vaultd down, its switch off, or `settings_unusable`: the reply says which, and no card is sent.

### 4.4 HTTP: `app/web/code_access.py`

Registered only when `CODE_ACCESS_ENABLED`. `POST` only, JSON in and out, under `/code/v1`.

| Route | Body | Answer |
|---|---|---|
| `/code/v1/status` | `{}` | `{"open": false}` or `{"open": true, "expires_at", "roots", "files_read", "files_left"}` |
| `/code/v1/tree` | `{"root": <rel>}` optional | vaultd's tree for the window's roots, narrowed to `root` when given (it must lie under a window root) |
| `/code/v1/file` | `{"path": <rel>}` | `{"path", "class", "sha256", "text"}` |

- **Auth:** `Authorization: Bearer <token>`; the bot compares `sha256(token)` with the configured hash using `hmac.compare_digest`. A missing or wrong token is a bare 404, the same as a route that does not exist. Ten wrong tokens in an hour lock all three routes for an hour (in memory, global, on the injected `Clock`, like `app/web/ratelimit.py`).
- **Window:** with a good token and no open window, `tree` and `file` answer 403 `{"error": "no_window"}`.
- **Confinement:** the bot checks the path against the window's roots before it asks vaultd, and passes the roots on so vaultd checks again. Outside the roots is the same 404 as a missing note.
- **Limits:** 120 requests a minute (global); `CODE_FILES_PER_WINDOW`.
- **Ledger and notice:** each successful `tree` or `file` call writes one `code_read` row (entry count or 1, and bytes). The first of a window sends one Telegram message, «Claude Code начал читать vault: окно до HH:MM», through the path the connector's read notice uses, and stamps `notified_at`.
- **Logs:** window id, route name, counts, status. Never the token, a path, a root or text. No exception text.
- New `VaultClient.code_tree` / `code_file` methods, parsed as strictly as the others. They are called from this module only.

### 4.5 The digest and `/state`

The daily digest that carries «Claude Code прочитал линзу: N раз» gains «Claude Code читал vault: N файлов, M запросов» when there were reads in its 24 hours. It is sent under the same conditions as the lens line, with "a window was open or there was a read" as this feature's trigger.

### 4.6 Tests

Window lifecycle (pending → open → expired / off / replaced; one open at a time; stale button); roots validation; the three routes (token, lockout, no window, confinement both ways, file cap, rate limit); the ledger and the single notice; digest line; `/delete` and export lists; debug views carry no roots; dark flag (`tests/test_dark_flags.py`): off, no route exists and `/vault code` says so; a log capture proving no path or text is logged; README command list (`tests/test_readme_commands.py`).

## 5. Claude Code's side

- Environment: `ANCHOR_CODE_URL` (`https://<bot host>/code/v1`) and the token. In a cloud environment the token is a network secret that the proxy injects as the `Authorization` header for the bot's host, so the session never holds it; in the CLI it is `ANCHOR_CODE_TOKEN`.
- Reads are plain `curl -sS -X POST "$ANCHOR_CODE_URL/tree" ...`. The guard hook already blocks a literal Railway host, which keeps the URL in the variable.
- CLAUDE.md gains the exception and its rules: only through `$ANCHOR_CODE_URL`; only while the user has opened a window (403 `no_window` means ask the user, never look for another way); read what the task needs and nothing else; the lens rule on text leaving the session applies; `Anchor/` and `never` stay closed; never call vaultd, the database or the connector for notes.
- `docs/claude-access.md` gains "The code window (CW1)": setup, the switch, the routes, what is logged. `docs/privacy.md`, `docs/vault-setup.md` (`VAULT_CODE_ACCESS`), `docs/decisions.md` and the README (status row, command list, a milestone section) follow.

## 6. Why it is safe enough

- **Two switches and a window.** The bot's env switch, vaultd's env switch, and a window only the user can open from Telegram. The standing token opens nothing by itself, like the lens role's password with `NOLOGIN`.
- **The class boundary stays in vaultd.** A bot bug, or a stolen token inside an open window, still cannot reach `never` or `Anchor/`.
- **Never silent.** A notice on the first read, a count in `/vault code`, a line in the digest.
- **Nothing accumulates.** The bot relays and forgets; the tables hold counts.
