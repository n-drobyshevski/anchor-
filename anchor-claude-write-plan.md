# Anchor — Claude writes knowledge notes (plan)

Version: 2026-09-26 (rev. 2, decisions settled) · Scope: Claude, through the Anchor connector, updates, creates and links **knowledge** notes in the Obsidian vault.
Parent docs: `anchor-claude-connector-plan.md`, `anchor-phase8-plan.md`, `anchor-phase8e-plan.md` §3, §7, §10. For this feature, **this file wins**. Every earlier invariant stays in force; nothing here loosens one.

Milestones are **W2a–W2b** (§10). §13's decisions are settled (rev. 2); the plan below already follows them.

---

## 0. Goal

When you and Claude find something new about a topic you keep a knowledge network on (CCRU, say), Claude updates the existing nodes itself, creates new ones when needed, and links them. There is no Inbox and no approval press per change.

In place of that press, the plan uses hard limits:
- a **boundary** Claude cannot cross: only notes whose class is `knowledge`, enforced by vaultd (§4);
- **compare-and-swap**, so Claude never overwrites an edit it has not seen (§6.1);
- **undo**, mostly from inside Claude (§6.2);
- **caps** on how much can change and how fast (§6.4);
- **content checks** before anything is written (§6.5);
- **one daily digest** that lists what changed (§6.7).

---

## 1. Where this sits

| Needs | Why | State today |
|---|---|---|
| **C3: the standing library switch** (`/claude library on\|off`) | Writing rides on the read switch (§5) | Built (#38) |
| **C3: `search_library`** | Claude has to find the nodes it edits | Built (#38): top 6, at least 2 shared lexemes |
| **Knowledge indexing in the sync pass** | Edited notes must be reindexed (§9) | Built (#37) |
| 8d's prompt-time retrieval | Not needed for writing | Not built (full-text rank failed its gate) |

The prerequisites exist, so W2 can be built.

The instruction filter on written text (§6.5) matters even though retrieval is not built. Claude's writes are knowledge notes, and the moment knowledge retrieval exists they reach the persona's prompt. An injected instruction must not be able to persist through a write and wait for that day.

---

## 2. Out of scope (do not build)

- **Delete** of any note (§13.1). Rename and move within knowledge folders are in scope (§3).
- Attachments, `.canvas`, `.base`, and any file that is not `.md`.
- **Personal notes**, in any mode (8e §10). `never`, unclassified, and settings-invalid notes stay invisible and unwritable.
- Anything under `Anchor/`, including `Anchor/settings.md`, fact files and journal pages.
- **Grok.** Grok never writes. This plan adds nothing to `/grok` or `app/web/mcp.py`.
- Writing from Telegram or the web UI.
- Changing a note's class. Claude cannot add, change or remove the `anchor:` property.

---

## 3. Tools

All of them live in `app/web/mcp_core.py` behind the Claude connection (C2's OAuth), and never on Grok's route. Each carries MCP annotations: `readOnlyHint: false`, `destructiveHint: true` for `update_note`, `rename_note` and `undo_changeset`, and `idempotentHint: false`. What claude.ai does with those is in §12.

| Tool | Arguments | Does | Returns |
|---|---|---|---|
| `update_note` | `path`, `new_body`, `base_hash` | Replaces the note's body (a full edit: rewrite and restructure allowed). Frontmatter is Anchor's to manage (§6.6). | `{path, hash, changeset_id}` |
| `create_note` | `folder`, `title`, `body` | Creates `<folder>/<title>.md`. The title is sanitised; an existing name is refused, never overwritten. | `{path, hash, changeset_id}` |
| `rename_note` | `path`, `new_path`, `base_hash` | Renames or moves a knowledge note to a free name in a knowledge folder, and rewrites `[[old]]` → `[[new]]` in every knowledge note linking to it, in the same changeset (§4) | `{path, hash, changeset_id, relinked}` |
| `get_note` | `path` | Read-only: the full body and hash of one knowledge note. Only while the write switch is on. | `{path, hash, body}` |
| `list_changes` | — | Recent changesets: id, time, the files' titles, and whether each is undone | a list, at most 20 |
| `undo_changeset` | `id` | Restores every file in that changeset from its stored pre-image (§6.2) | `{restored, refused}` counts |

- **Reading before writing.** `update_note` and `rename_note` need a `base_hash`, which always comes from a read, never from a guess. C3's `search_library` returns text only. **While the write switch is on**, each result also carries the note's `path` and `hash`, and `get_note(path)` returns a whole note. With writing off, nothing about paths reaches Claude, exactly as in C3.
- **Links** are ordinary `[[wikilinks]]` inside bodies; there is no `link_notes` helper (§13.3).
- **One refusal text.** Every refusal returns `isError: true` with the same text:
  > Запись отклонена.

  The text never says why and never echoes a path, so Claude cannot probe the boundary by reading refusals. The bot logs the reason code (§9). A write switch that is off answers differently, because telling you how to open it leaks nothing:
  > Запись в библиотеку выключена. Включи в Telegram: /claude library write on

---

## 4. vaultd is the boundary

The bot passes writes through, and vaultd decides. Whatever the bot's code does, vaultd refuses a write outside knowledge. That is the same shape as 8a's rule for `Anchor/`, and vaultd's own tests pin it, independently of the bot.

**New endpoints**, bearer-authenticated like the rest:

| Route | Does |
|---|---|
| `PUT /v1/knowledge?path=` body `{content, if_sha256, changeset}` | Update (`if_sha256` required) or create (`if_sha256: null`, create-only via `os.link`, as for Anchor files) |
| `GET /v1/changes` | The undo store's index: changeset ids, times, paths, hashes (paths go to the bot for the digest's titles and to Claude through `list_changes`) |
| `POST /v1/knowledge/rename` body `{path, new_path, if_sha256, changeset}` | Rename or move, with backlinks (below) |
| `POST /v1/undo?changeset=` | Restore that changeset's pre-images (§6.2) |

**What vaultd accepts, all checked at write time, inside the store's lock:**
1. **The path.**
   - `.md` only; no dot segments; no symlinks (8a's `O_NOFOLLOW` walk);
   - never under `Anchor/`, never `Anchor/settings.md`;
   - a new file only into an **existing folder** whose folder rule is `knowledge`;
   - the new file's name is not taken.
2. **The existing file's effective class is `knowledge`**, by 8e's resolution: folder rules plus the note's own property, stricter class wins, unusable settings means nothing is writable. A note Claude may read but whose class is personal is not writable.
3. **The new content's class is `knowledge` too.** vaultd resolves the class of the text it is about to write, with the same function. The new body cannot change the class: its frontmatter `anchor:` must equal the old one (or be absent in both). So a write can neither move a note out of knowledge nor smuggle a personal mark in to hide it later.
4. **Compare-and-swap** on `if_sha256` for updates, as for Anchor files. On a mismatch, 412.
5. **Size:** at most `KNOWLEDGE_WRITE_MAX_BYTES` (64 KB, a constant).
6. **UTF-8** and a frontmatter that parses with vaultd's strict loader, or no frontmatter at all.

**Refusals are indistinguishable.** Every one returns the same 403 with an empty body, except CAS (412) and a missing file on update (404, identical to 8a's read refusal). The bot turns all three into «Запись отклонена».

**Same lock, same residual race.** Knowledge writes take the store's single `asyncio.Lock`, like Anchor writes. The one race 8a documents also applies: `ob` can replace a file between vaultd's re-check and `os.replace`. It is closed the same way: the next read sees the new hash, and Claude's next write fails CAS.

**Rename and backlinks.** `rename_note` goes through these checks:
- **Source and destination:** the source must pass 1–4 above. The destination must be a free `.md` name in an existing knowledge folder.
- **Finding the links:** vaultd resolves links the way Obsidian does, by basename: `[[old]]`, `[[old|label]]`, `[[old#heading]]` and `![[old]]`.
- **Refusals:** the rename is refused if the old basename is ambiguous (two notes share it), if **any** note outside knowledge links to it (a personal note is never read back to Claude and never written), or if the files touched would exceed the per-changeset cap (§6.4).
- **Recording:** the rename, and every rewritten backlink, are recorded in one changeset, so one undo restores all of them.
- vaultd reads non-knowledge notes only to answer "does it link here", inside the process. Their content never leaves vaultd.

**The token.** The write routes take the same `VAULT_API_TOKEN` as the rest of vaultd (§13.7). One variable fewer; a leaked token could write knowledge notes, which the class boundary, the caps and undo still bound.

---

## 5. Switches

There are no windows for the library, only standing switches on the current connection:

| Switch | Default | Set by | Turned off by |
|---|---|---|---|
| `library_read` (C3) | off | `/claude library on\|off` | `/claude library off`, `/revoke`, `/claude disconnect`; wiped by `/delete`; a new connection starts off |
| `library_write` (this plan) | off | `/claude library write on\|off` | the same, and also **turning read off** |

- **Writing has its own switch**, off by default, and requires the read switch (§13.2).
- `/claude` shows both: «Библиотека: чтение вкл · запись выкл».
- **Grok never writes.** No grant, scope or switch reaches `/grok`.
- The switch lives on `oauth_connection` (two booleans), not on `access_grant`. It follows the connection's 30-day life and dies with it.

---

## 6. Safety in place of approval

### 6.1 Compare-and-swap

- `update_note` carries the `base_hash` Claude read.
- If the file changed on disk since (your phone, Obsidian Sync), vaultd answers 412, the write is refused, and nothing is merged.
- Claude can read again and retry. The retry is a new write under the same caps.

### 6.2 Undo, mostly inside Claude

**The undo store** lives in vaultd, outside the synced vault: `/data/anchor-undo/`, never under `/data/vault`. Railway gives a service one volume (plan 8 §2), so "a volume of its own" is not possible. A directory next to the vault on the same volume is the nearest thing, and `ob` never syncs it (decision §13.9).

For each changeset, the store keeps:
- per file: the path, the **pre-image bytes** (or "absent" for a created file), and the **hash of what was written**;
- the changeset's time.

It is kept for **`UNDO_TTL_DAYS = 14`**, a constant. The argument is in §8.5.

**`undo_changeset(id)` and `/claude undo`:**
- Restore, per file, only if the file on disk still hashes to what Claude wrote (compare-and-swap). If you edited it since, that file is refused and counted, and the rest are restored.
- A created file is undone by deleting it, and only if it is unchanged.
- A rename is undone file by file: the new path back to absent, the old path back to its pre-image, and each rewritten backlink back to its pre-image. Each file is compare-and-swap checked like any other.
- **Undo writes only stored pre-images.** It takes no content argument. There is no code path by which it writes anything else, and vaultd's tests pin that.
- An undo is recorded as its own changeset (so the digest shows it). **An undo cannot itself be undone**: that would write Claude's text back without any of §6.5's checks.
- `/claude undo` in Telegram undoes the most recent changeset. `/claude undo all` undoes the last 24 hours. The digest's button does the same.

### 6.3 What a changeset is

A changeset is all writes made by one connection within a **10-minute idle window**. A new one starts after 10 minutes without a write. That groups a burst like "extend two nodes and create a linked third" into one undoable unit.

Settled (§13.4); the alternative, one changeset per tool call, was not chosen.

### 6.4 Caps, as constants

A deploy cannot loosen these; they live in `app/core/claude_write_limits.py` and vaultd's own copy:

| Cap | Value |
|---|---|
| Files per changeset | 5 |
| Changesets per hour | 4 |
| Files created per day | 10 |
| Bytes per file | 64 KB |
| Bytes written per connection per day | 512 KB |
| Undos per hour | 4 |

Over a cap, the write is refused. The digest counts refusals, so a runaway loop shows up.

### 6.5 Content checks before a write

These run in the bot, before the PUT, on the whole new body:
- **Instruction filter:** §7.1's instruction ids (`override_previous*`, `system_prompt`, `developer_mode`, `role_tag`, `exfiltrate`, `role_reassign`, `speak_as_assistant`), with no rule exemption. A hit refuses the write. `url`, `handle` and `code_fence` are not refused: a knowledge note legitimately holds links and code.
- **Secrets:** `redact.secret_spans` plus `app/vault/secrets.py` (8d's list). A hit **refuses** the write rather than masking it. Masking would change Claude's text behind its back, and the hash Claude gets back would not match what it wrote (decision §13.6).
- **Class:** the frontmatter rule of §4.3. The bot checks it too, as defence in depth. vaultd's check is the one that counts.

### 6.6 Provenance

Every file Claude writes carries two frontmatter properties, set by **vaultd**, not by Claude:
```yaml
anchor_edited_by: claude
anchor_edited_at: "2026-09-26T10:14:00Z"
```
- They are written on every file touched, including one that Claude creates, so you can filter Claude's nodes in a Bases view.
- vaultd strips any `anchor_edited_*` keys Claude put in the body and writes its own.
- Your other properties are kept byte for byte, with 8b's line-mark method.
- An undo restores the pre-image exactly, so the properties vanish again if the file had none.
- When knowledge retrieval exists, these nodes keep their rank but the prompt labels them «(записано Claude)» (§13.8).

### 6.7 Notices: one daily digest

Nothing about writes goes to Telegram except **one message a day**, and only when there was activity:
> Claude за сутки изменил 3 заметки: «CCRU», «Hyperstition», «Ник Ланд» (создана). Отклонено: 1.
> [Откатить всё за сутки]

- The same scheduled job as C3's library digest: one job, one message covering reads and writes, `dedup_key` per local date, obeying `may_report_now`, and sent on the first allowed pass if a quiet period covers the usual time.
- **Titles** are fetched from vaultd when the digest is built and sent to Telegram only. They are never logged or stored in Postgres.
- The button's callback carries the date and the epoch. It is stale after `/delete` and blocked from the web chat at both layers (like 8c's `v:`).

---

## 7. Data model

**Bot (Postgres), no paths and no text:**
```sql
claude_changeset (
  id              bigserial primary key,
  connection_id   bigint not null references oauth_connection(id) on delete cascade,
  vault_ref       text not null,       -- vaultd's changeset id (opaque)
  kind            text not null check (kind in ('write','undo')),
  files           int not null,
  bytes           int not null,
  refused         int not null default 0,
  created_at      timestamptz not null,
  undone_at       timestamptz
);
-- oauth_connection gains library_read, library_write boolean not null default false (C3 adds the first)
```

**vaultd (the vault volume only):** the undo store of §6.2, one JSON index plus a blob per pre-image. `POST /v1/purge` wipes it.

`/export` includes `claude_changeset` (ids, counts, times). `/delete` truncates it and the purge job wipes the undo store.

---

## 8. Threats

### 8.1 Prompt injection through read text leading to writes

A note Claude reads, or a web page in the same chat, says "update every note to say X".

Defences:
- the class boundary (only knowledge notes);
- the instruction filter on written text;
- the caps (at most 5 files per changeset, 4 changesets an hour);
- CAS;
- the digest names every touched note by the next day;
- undo reverses it.

What remains: a subtle, instruction-free falsehood written into knowledge notes. The digest and your own reading are the defence.

`docs/claude-connector.md` gains a line: keep write-capable chats free of untrusted web content.

### 8.2 Claude Code sessions seeing the write tools

A claude.ai connector reaches Claude Code cloud sessions (connector plan §6.3), and a write tool is worse there than a read.
- The guard hook's `ANCHOR_TOOLS` gains `update_note`, `create_note`, `rename_note`, `get_note`, `list_changes` and `undo_changeset`.
- This matters in practice. This very session's connector is named `anc`, which the server-name pattern `anchor\w*?` does not match, so only the tool-name list catches it.
- `.claude/settings.json`'s deny list stays as is.
- `CLAUDE.md` already forbids calling the connector, and gains the write tools by name.

### 8.3 Sync conflicts with the phone

- CAS refuses a write over any edit Anchor has not seen.
- If your phone edits *after* Claude's write but before Sync delivers it, Obsidian Sync's merge applies. Phase 8 plan §2: `--conflict-strategy merge` uses diff-match-patch, and the result is a merged file.
- Claude's next write then fails CAS. Undo of that file is refused too, because its hash no longer matches what Claude wrote, so undo never throws away your part of a merge.

### 8.4 A runaway loop of writes

Caps (§6.4) stop it within the hour: at most 20 files, then refusals. The digest's «Отклонено: N» shows it. `/claude library write off` stops it at once.

### 8.5 A malicious rewrite sitting unnoticed until undo has expired

This matters more because no write is announced on its own.
- **Cadence:** the digest is daily, so every write is named within about 24 hours.
- **TTL 14 days** covers a digest you miss for a week plus a week to act.
- The last net is Obsidian Sync Standard's version history, 1 month (§18.3 of the Phase 8 plan). It sits outside Anchor and stays reachable after undo has expired.
- A longer TTL keeps more copies of your knowledge text on the vault volume, which is the cost. 14 days (§13.5).

### 8.6 Injected text telling Claude to call `undo_changeset`, or to write in a loop

- Undo can only put back **your** earlier text (a stored pre-image), and only where Claude's text is still in place. The worst an injected undo does is revert Claude's own recent work.
- An undo cannot be undone.
- Undos have their own cap (4 an hour) and appear in the digest.
- A write loop is §8.4.

---

## 9. Interplay

- **8c** ingests only `Anchor/` fact and journal files, so Claude's knowledge edits never become memories.
- **Knowledge indexing** (a W2 prerequisite) reindexes a note whose hash changed, like any other edit.
- **`/delete`** purges the undo store (through `vault_purge`), truncates `claude_changeset`, and resets both switches with the connection.
- **`/export`** includes `claude_changeset` (ids, counts, times; no text, no paths).
- **Logs** carry ids, counts, reason codes and byte sizes. They never carry a path, title, heading or text, in the bot or in vaultd.
- **Isolation.** The write path imports `notes_knowledge` only where 8e §8's allowlist says, with a row citing this plan. `notes_personal` stays unreachable from `app/web/`, pinned by the AST test.

---

## 10. Milestones

| Milestone | Contents |
|---|---|
| **Prerequisites** | C3 (switch, `search_library`, digest) and knowledge indexing in the sync pass |
| **W2a. vaultd** | `PUT /v1/knowledge`, `/v1/changes`, `/v1/undo`, the undo store, provenance, caps (vaultd's copy), purge. Tests in vaultd only. Bot untouched. |
| **W2b. Bot** | The five tools, the write switch, the bot-side caps and content checks, `claude_changeset`, `/claude undo`, the digest's write lines and button, the guard hook, `CLAUDE.md`, `docs/claude-connector.md`, and the `/privacy` line if needed |

**Manual check (W2b):**
1. Turn writing on.
2. Ask Claude to extend a CCRU node and create a linked node.
3. Check both in Obsidian, with their provenance properties.
4. Ask «откати это»: both files return exactly.
5. Edit a node on your phone while Claude writes to it: the write is refused.
6. The next day's digest lists the changes.

---

## 11. Tests (required)

**vaultd (W2a):**
- **Refusals**, each with its own test:
  - a personal note, a `never` note and an unclassified note;
  - a note in a knowledge folder marked `anchor: personal`;
  - with invalid settings, everything is refused;
  - a path under `Anchor/`, and `Anchor/settings.md`;
  - a non-`.md` file, a dot segment and a symlink;
  - a new file in a non-knowledge folder, and in a folder that does not exist;
  - a name that is taken;
  - frontmatter that changes `anchor:`;
  - oversize content;
  - a CAS miss;
  - each vaultd cap;
  - every refusal body is byte-identical.
- **Rename:**
  - it moves the file and rewrites every backlink form (`[[x]]`, `[[x|l]]`, `[[x#h]]`, `![[x]]`) in knowledge notes;
  - it is refused when a personal, `never` or unclassified note links to the note, and that note is left byte-identical;
  - it is refused when the basename is ambiguous, when the destination is taken or not knowledge, and when the files touched exceed the cap;
  - one undo restores the file and all its backlinks byte for byte.
- **Undo:**
  - restores byte for byte;
  - writes only stored pre-images (no route accepts content);
  - is refused per file after a later edit;
  - a created file is deleted only if unchanged;
  - an undo of an undo is refused;
  - the TTL expires a changeset (frozen clock);
  - purge wipes the store.
- **Provenance:** the properties are set, Claude's own `anchor_edited_*` keys are stripped, and other properties stay verbatim.
- **Logs:** no path, title or text in any record.

**Bot (W2b):**
- the write switch off, the read switch off, Grok's route, and a revoked connection all refuse;
- each bot cap;
- the instruction filter and the secret refusal;
- one refusal text, with no path in it;
- the digest: content, the `may_report_now` gate, its button's epoch and staleness, and the web chat blocked at both layers;
- `/claude undo` and `undo all`;
- `/delete` wipes both switches, `claude_changeset` and the undo store;
- `/export` carries no path or text;
- the isolation AST test;
- the guard hook blocks all five tools under any server name;
- wiring through `app/main.py`'s own builders.

Each test is proven by a deliberate breaking edit, then reverted, and listed in the PR.

---

## 12. Verify before coding, and report

1. **Railway:** still one volume per service. If that changed, give the undo store its own volume.
2. **claude.ai and MCP annotations:** does `destructiveHint` make claude.ai ask before each call? If it does, the "no approval press" goal meets a client-side confirm, which is harmless but should be known.
3. **Obsidian Sync:** check the merge behaviour on a concurrent edit against obsidian-headless's current version.
4. **The connector reaching Claude Code** (connector plan §11): can a connector be kept out of Claude Code sessions and routines? If so, turning that off is the strongest defence of §8.2.

---

## 13. Decisions (settled, rev. 2)

| § | Decision |
|---|---|
| 13.1 | No delete. **Rename and move are allowed** within knowledge folders, with backlinks rewritten (§3, §4). |
| 13.2 | **Its own write switch**, `/claude library write on\|off`: off by default, needs the read switch, and turning read off turns write off |
| 13.3 | **Wikilinks only**, no `link_notes` |
| 13.4 | A changeset is a **10-minute idle window** |
| 13.5 | Undo TTL **14 days** |
| 13.6 | A secret in written text **refuses** the write |
| 13.7 | The **same token** (`VAULT_API_TOKEN`) for the write routes |
| 13.8 | Retrieval, when it exists, **labels** Claude's nodes «(записано Claude)» |
| 13.9 | The undo store is on the **vault volume, outside the vault root** (`/data/anchor-undo/`) |
| 13.10 | C3 shipped `search_library` as top 6 with at least 2 shared lexemes, with no rank threshold (#38) |

One addition follows from 13.1: `get_note(path)`, and paths plus hashes in `search_library` results, **only while the write switch is on** (§3). Otherwise Claude could not name the note it edits.
