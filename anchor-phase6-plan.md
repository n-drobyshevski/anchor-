# Anchor — Phase 6 Implementation Plan

Version: 2026-09-23 · Scope: v2 plan build steps 9 and 11 (idle learning and hardening), **re-planned for OpenRouter** (no batch discount)
Parent docs: v2 plan, Phase 1–5 plans, hardening pass. For Phase 6 work, **this file wins**. Earlier invariants stay in force unless §13 amends them.

---

## 0. Goal

1. **Idle learning.** When the user is silent, Anchor spends spare daily budget on background work that makes it sharper:
   - memory consolidation;
   - deeper reflection;
   - pre-drafted morning notes;
   - self-critique;
   - research on user-chosen topics;
   - a weekly regression canary.
2. **Guardrails.** Everything idle is **visible (`/digest`), budgeted, reversible, and never touches live state**. Learned *content* reaches live chat only through existing gates: cards stay pending until adopted, and notebook entries are visible and closable.
3. **Hardening:**
   - encrypted off-site backups with a tested restore;
   - retention sweeps;
   - a `/privacy` note;
   - dependency auditing;
   - a scheduler liveness check.

**Budget note.** The v2 plan relied on xAI's 20% batch discount. OpenRouter has none, so idle work runs at normal per-token prices. Cydonia and the safety model are cheap, so realistic idle spend is still only cents per day (§11). More budget does not mean better learning: every job type has its own daily limit, and the useful work saturates quickly.

## 1. Out of scope (do not build)

- Tracker/device integrations (Phase 7).
- Any idle job that sends messages. Idle is silent; `/digest` is pull-only.
- Autonomous topic choice for research (topics come only from the user, §6.5).
- Automatic persona or amendment changes.
- Automatic adoption of anything.
- pgvector, streaming, a web UI.

---

## 2. New config

```
IDLE_ENABLED=true
IDLE_AFTER_H=3                    # user silent at least this long
IDLE_USD_CAP=0.25                 # idle spend per local day
IDLE_RESERVE_USD=0.50             # always kept free for live chat: idle runs only if spent_today + est <= DAILY_USD_CAP - RESERVE
IDLE_JOB_USD_CAP=0.05             # per job
IDLE_MAX_JOBS_PER_DAY=8
IDLE_WINDOW=00:00-23:59           # local hours idle may run (default all day)
IDLE_UNDO_DAYS=7
CRITIQUE_SAMPLE=5                 # recent persona replies scored per critique run
CANARY_DOW=3                      # ISO weekday for the weekly eval canary
BACKUP_ENABLED=true
BACKUP_TIME=04:00                 # local
BACKUP_KEEP_DAILY=14
BACKUP_KEEP_WEEKLY=8
BACKUP_AGE_RECIPIENT=age1...      # public key only; the private key never touches the server
BACKUP_S3_ENDPOINT=
BACKUP_S3_BUCKET=
BACKUP_S3_ACCESS_KEY_ID=
BACKUP_S3_SECRET_ACCESS_KEY=
UPDATE_PAYLOAD_RETENTION_DAYS=30  # null telegram_update.payload after N days
JOB_RETENTION_DAYS=30             # delete done/failed/cancelled job rows after N days
MESSAGE_RETENTION_DAYS=0          # 0 = keep forever; >0 deletes messages older than N days whose scene is summarized
```

**New dependencies, all to be confirmed with me before adding:**

| Library | Purpose | Links |
|---|---|---|
| pyrage | age encryption without a binary | [PyPI](https://pypi.org/project/pyrage/) · [GitHub](https://github.com/woodruffw/pyrage) |
| boto3 | S3-compatible upload | [PyPI](https://pypi.org/project/boto3/) · [GitHub](https://github.com/boto/boto3) |
| pip-audit | CI only, dev dependency | [PyPI](https://pypi.org/project/pip-audit/) · [GitHub](https://github.com/pypa/pip-audit) |

`pg_dump` must be available in the container: the Postgres client, matching the server major version.

**Backup target:** any S3-compatible bucket. Verify whether Railway offers buckets on this account; otherwise use Cloudflare R2 or Backblaze B2. Ask me which.

---

## 3. Data model

```sql
idle_run (
  id           bigserial primary key,
  kind         text not null check (kind in
               ('backfill','consolidate','reflect','prebrief','critique','research','canary')),
  local_date   date not null,
  status       text not null default 'queued',   -- queued|running|done|failed|skipped|undone
  skip_reason  text,
  usd_cost     numeric(10,6) not null default 0,
  summary      jsonb not null default '{}',      -- counts and scores only, never text
  reversible   boolean not null default false,
  started_at   timestamptz, finished_at timestamptz, undone_at timestamptz,
  created_at   timestamptz not null default now()
);

idle_change (                                    -- undo log
  id          bigserial primary key,
  run_id      bigint not null references idle_run(id) on delete cascade,
  table_name  text not null check (table_name in ('memory','notebook_entry')),
  row_id      bigint not null,
  op          text not null check (op in ('insert','supersede','close','update')),
  before      jsonb,                             -- prior row state for update/close/supersede
  created_at  timestamptz not null default now()
);

brief_note (
  local_date  date primary key,                  -- the morning it is for
  notes       jsonb not null,                    -- max 3 strings, each <= 160
  created_at  timestamptz not null default now(),
  used_at     timestamptz
);

interest_topic (
  id          bigserial primary key,
  text        text not null check (length(text) <= 100),
  packet      text not null check (packet in ('forums','guides','ref')),
  active      boolean not null default true,
  last_run_at timestamptz,
  created_at  timestamptz not null default now()
);

backup_log (
  id          bigserial primary key,
  started_at  timestamptz not null, finished_at timestamptz,
  object_key  text, bytes bigint, sha256 text,
  status      text not null,                     -- ok|failed|pruned|purged
  error_code  text
);
```

- `/export` covers `idle_run` (metadata), `idle_change`, `brief_note`, and `interest_topic`. It doesn't need `backup_log`.
- `/delete` purges all of them, **and** purges every backup object (§9.3).
- Extend the coverage tests.

---

## 4. Eligibility and budget (`core/idle/gate.py`) — a pure function

```
idle_gate(kind, state, now, spend, counts, facts, config) -> (allowed, reason)
```

Checks run in order; the first failure wins.

| # | Check | Reason |
|---|---|---|
| 1 | `IDLE_ENABLED` false | `disabled` |
| 2 | `persona_active=false` (pause or welfare) | `paused` |
| 3 | `welfare_at` within 24h | `welfare_cooldown` |
| 4 | `last_user_msg_at` within `IDLE_AFTER_H` | `user_active` |
| 5 | local time outside `IDLE_WINDOW` | `window` |
| 6 | an idle job is already running or queued | `busy` |
| 7 | idle jobs today ≥ `IDLE_MAX_JOBS_PER_DAY` | `max_jobs` |
| 8 | idle spend today + `IDLE_JOB_USD_CAP` > `IDLE_USD_CAP` | `idle_cap` |
| 9 | total spend today + `IDLE_JOB_USD_CAP` > `DAILY_USD_CAP − IDLE_RESERVE_USD` | `reserve` |
| 10 | kind-specific limit (§6) | `kind_rule:<detail>` |

The gate runs at planning time **and** again inside the job before its first model call.

**Preemption:** if a user update arrives while an idle job is running, the job finishes its current model call, **commits nothing that isn't already committed**, and ends with status `skipped` and reason `preempted`. Live handling always goes first; the worker already claims inbound updates before jobs.

Spend is ledgered under category `idle:<kind>`, and it also counts toward the global daily cap.

---

## 5. Planner (`core/idle/planner.py`)

The Phase 3 heartbeat calls `plan_idle()` every 60 s. It picks the **first** kind in priority order whose gate passes and enqueues exactly one job (`dedup_key=idle:<kind>:<local_date>:<n>`).

Priority:
1. `backfill`
2. `consolidate`
3. `prebrief` (only after 19:00 local)
4. `reflect`
5. `critique`
6. `research`
7. `canary` (only on `CANARY_DOW`)

One idle job runs at a time, and the planner never enqueues a second while one is queued or running.

---

## 6. Idle job types

All of them run with **no tools and no plugins** except `research`, which reuses the Phase 4 pipeline. All of them exclude welfare, OOC, and canned rows from every input.

### 6.1 `backfill` (max 3/day)

Runs the work that was deferred by the cap or skipped: scene summaries with `summary IS NULL` and ≥3 messages, and notebook reflections missing for closed scenes. This reuses the existing jobs verbatim. The kind rule is "there is nothing to backfill".

### 6.2 `consolidate` (max 1/day, reversible)

**Model:** `LLM_MODEL_SAFETY`, strict JSON.

**Input:** clusters of active memories with `source='extractor'`, unpinned, kinds `identity`, `preference`, `event`, where pairwise `similarity > 0.45`. At most 5 clusters of up to 6 memories each, with IDs.

**Output:**

```json
{"merges": [{"ids": [1,2], "text": "≤300", "kind": "identity|preference|event"}],
 "contradictions": [{"keep_id": 3, "drop_id": 4}]}
```

**Code rules:**
- Every ID must be in the input set.
- **Never touch** memories with `source='user'` or `source='adopt'`, pinned memories, rules, or techniques.
- A merge inserts a new memory (`source='consolidate'`) and supersedes the originals.
- A contradiction supersedes `drop_id` with `keep_id`.
- Run the risk rules, injection scan and redaction on merged text.
- Log each change to `idle_change` with its `before` state.

The kind rule: fewer than 2 clusters means skip.

### 6.3 `reflect` (max 1/day, reversible)

**Model:** `LLM_MODEL_SAFETY`, strict JSON. This is a deeper version of the Phase 5 per-scene reflection.

**Input:** the last 7 days of scene summaries, journal lines, check-ins and order results, plus active notebook entries.

**Output:** the Phase 5 §6 schema (add observations or threads, close or update Anchor's own entries). The **same validator**, caps and ownership rules apply, and every change is logged to `idle_change`.

The kind rule: skip if no new scene summary has appeared since the last reflect run.

### 6.4 `prebrief` (max 1/day, after 19:00 local)

**Model:** `LLM_MODEL_SAFETY`, strict JSON.

**Input:** today's check-in and order results, the due action, open threads, and tomorrow's due orders.

**Output:** `{"notes": ["≤160", "... max 3"]}` for tomorrow's local date, validated with risk rules, injection scan and redaction. Stored in `brief_note`.

It's **used only** by the Phase 3 morning outbound, which injects the notes as a hidden flag (`Заметки к утру: …`) and sets `used_at`. The notes expire unused after their date.

It never changes whether a morning message is sent; the outbound gate alone decides that. The kind rule: skip if the morning intent is disabled or a note for tomorrow already exists.

### 6.5 `research` (max 1/day, topics from the user only)

- **Topics** come only from `interest_topic` rows the user created with `/interests add <packet> <тема>`.
- The planner picks the active topic with the oldest `last_run_at`.
- Runs the **unchanged Phase 4 `/study` pipeline**, producing pending cards.
- It shares the **daily research quota**: it runs only if the user hasn't used their `/study` today. The kind rule covers `RESEARCH_ENABLED=false`, no topics, and quota used.
- No completion message is sent. The cards appear in `/notes` and `/digest`.

### 6.6 `critique` (max 1/day, report only)

**Model:** `LLM_MODEL_JUDGE` (it must differ from the persona model; otherwise skip with `no_independent_judge`).

**Input:** the last `CRITIQUE_SAMPLE` persona replies (chat and outbound, never neutral or welfare) with their preceding user message, scored on the Phase 3 rubric (voice, one next action, boundaries, pressure/escalation, third parties).

**Output:** strict JSON scores only. `idle_run.summary` stores the aggregates and the IDs of replies scoring below 4 on boundaries or pressure. **No model text is stored.**

- The weekly review analysis (Phase 5 §8) receives last week's critique aggregates as data.
- Nothing changes automatically.

### 6.7 `canary` (weekly on `CANARY_DOW`)

Runs the Phase 5 **blocking eval subset** in-process (same runner as `amendment_trial`) against the current persona, amendments and models, with the independent judge. It stores pass/fail per case.

Any failure makes `/digest` show `⚠️ Регрессия: кейсы …`. This catches silent provider or model drift on OpenRouter.

It's skipped if there's no independent judge.

---

## 7. `/digest` and undo

`/digest` (optionally `/digest 7d`, default 24h) sends a plain, not in-character, message:

```
Фоновая работа за 24 ч — $0.07
• Сводки: догнала 2
• Память: 3 объединения, 1 противоречие  [Отменить]
• Заметки: +2, закрыто 1                 [Отменить]
• Утро: заметки готовы
• Самопроверка: 5 ответов, ниже нормы — 0
• Поиск: «тема» → 4 карточки (/notes)
• Канарейка: ок
Пропуски: user_active ×6, idle_cap ×1
```

- **[Отменить]** (callback `idle:u:<run_id>`) is available for runs with `reversible=true`, `status='done'`, less than `IDLE_UNDO_DAYS` old, and not yet undone.
- **Undo** replays `idle_change` in reverse:
  - inserted rows are deleted;
  - superseded pointers are cleared;
  - closed entries are reactivated;
  - updated rows are restored from `before`.

  It runs in one transaction, sets `status='undone'`, and writes `state_change` with source `undo`.
- If a row was changed by something else after the idle run, the undo for that row is skipped and reported: «часть изменений уже перезаписана».

**`/interests`** lists topics with `[✖]`. `/interests add <forums|guides|ref> <тема>` validates the text with the risk rules (a `high` hit gets «Такое не ищу.») and caps active topics at 10.

---

## 8. Invariants for idle work

- Idle jobs **never**:
  - send messages;
  - change `intensity`, `focus_on`, `due_action`, `streak`, `persona_active`, or outbound state;
  - write `persona.md`, amendments, standing orders, rules, or techniques;
  - adopt cards.

  Enforce with AST import-restriction tests over `core/idle/`.
- Idle writes are limited to: `memory` (consolidate, `source='consolidate'`, reversible), `notebook_entry` (reflect, reversible), `brief_note`, `idle_run`/`idle_change`, research cards (pending), plus the reused backfill jobs.
- **Nothing idle runs while the persona is paused, during welfare cooldown, or within `IDLE_AFTER_H` of user activity.** A user update preempts a running job.
- Idle can never consume the live-chat reserve.
- Welfare, OOC, and canned rows never enter idle inputs.
- Logs and `idle_run.summary` contain no text: IDs, counts, scores, codes and cost only.

---

## 9. Hardening

### 9.1 Encrypted backups (`ops/backup.py`)

- A nightly job at `BACKUP_TIME` local, scheduled by the heartbeat. It isn't an idle job, isn't budget-gated, and doesn't depend on the persona state.
- Steps:
  1. `pg_dump --format=custom` streamed through **age encryption to `BACKUP_AGE_RECIPIENT`** via pyrage;
  2. upload to `s3://<bucket>/anchor/<YYYY>/<MM>/<DD>/anchor-<ts>.dump.age`;
  3. record bytes, sha256 and status in `backup_log`.
- **Plaintext never touches disk.** Stream it, or use a tmpfs pipe if streaming isn't possible, and say which.
- **Only the public key is on the server.** The private key stays offline with the user, which means a leaked bucket or server doesn't expose backups.
- **Pruning:** keep `BACKUP_KEEP_DAILY` daily plus `BACKUP_KEEP_WEEKLY` Sunday backups; delete the rest and log them as `pruned`.
- A failure writes `backup_log.status='failed'` with an error code, and `/state` shows `Бэкап: ⚠️ ошибка <дата>`.

### 9.2 Restore runbook and test

- `docs/restore.md` covers: download, `age -d -i key.txt`, `pg_restore` into a fresh database, point `DATABASE_URL`, run migrations, smoke test.
- `scripts/restore_check.py` takes a local private key path, downloads the latest backup, decrypts it, restores into a **local throwaway** Postgres, and asserts the row counts of the key tables. It's run manually, and the first successful run is logged in the milestone report.

### 9.3 `/delete` amendment

- In addition to the Phase 2 wipe, `/delete` **deletes every backup object** under the prefix and logs them as `purged` (metadata only).
- The confirm text changes to:
  > Удалить все данные и все резервные копии? Это необратимо.
- The final reply changes to:
  > Удалено, включая бэкапы. Копии у провайдеров моделей удаляются по их правилам хранения.

### 9.4 Retention sweeps (daily)

- `telegram_update.payload := null` after `UPDATE_PAYLOAD_RETENTION_DAYS`.
- `job` rows with a terminal status are deleted after `JOB_RETENTION_DAYS`.
- If `MESSAGE_RETENTION_DAYS > 0`: delete messages older than that **only if** their scene has a summary. Scenes and summaries stay.
- The existing Phase 4 clip-text sweep and Phase 5 expiries are unchanged.

### 9.5 `/privacy`

Fixed Russian text, 8–10 lines, stating:
- what is stored and where (Railway Postgres);
- that model calls go through OpenRouter with data-collection deny, and that providers keep data per their own policies;
- that Telegram bot chats are not end-to-end encrypted;
- backups: encrypted, 14 daily plus 8 weekly;
- clip text is kept 30 days;
- `/export` and `/delete`.

Also write `docs/privacy.md` in English with the same content.

### 9.6 Liveness and supply chain

- `/readyz` fails if the heartbeat hasn't run in the last 5 minutes, so Railway restarts the service.
- CI runs `pip-audit` (failing on known vulnerabilities with a fix available) and gitleaks.
- Enable Dependabot for pip and GitHub Actions.
- `docs/secrets.md` is the rotation runbook for the Telegram token, OpenRouter key, S3 keys, and the webhook secret.

---

## 10. Commands (additions)

| Command | Behavior |
|---|---|
| `/digest [24h\|7d]` | §7 |
| `/interests`, `/interests add …` | §7 |
| `/privacy` | §9.5 |
| `/state` | adds idle spend today / cap, idle jobs today, last backup status and time, canary status |

---

## 11. Cost (OpenRouter, no batch)

| Job | Typical cost |
|---|---|
| backfill | about $0.005 each |
| consolidate | about $0.005 |
| reflect | about $0.01 |
| prebrief | about $0.003 |
| critique | about $0.01–0.03 (depends on judge price) |
| research | about $0.01–0.05 |
| canary | about $0.05, weekly |

A typical day comes to **about $0.05–0.12**, far under `IDLE_USD_CAP=0.25`. The live reserve of $0.50 keeps chat safe even on busy days. Raising the caps gives little benefit, because the per-kind daily limits bind first.

Backup storage for a single user is well under 1 GB, so negligible.

---

## 12. Tests (required)

- **idle gate:** table-driven over every row of §4, including the reserve math, window, and preemption reason.
- **planner:** priority order; one job at a time; `prebrief` only after 19:00; `canary` only on `CANARY_DOW`; per-kind daily limits.
- **preemption:** a user update mid-job → `skipped:preempted` with no partial writes.
- **consolidate:**
  - never touches user, adopt, pinned, rule or technique memories;
  - IDs must be from the input set;
  - supersede chains are correct;
  - validators run;
  - undo restores exactly the prior state;
  - partial-conflict undo is reported.
- **reflect:** reuses the Phase 5 validator; ownership respected; undo.
- **prebrief:** only the morning outbound reads it; expiry; it doesn't affect the gate decision.
- **research:** user topics only; shared quota; unchanged pipeline; no completion message.
- **critique:** no text stored; skipped without an independent judge; aggregates reach the review input.
- **canary:** pass/fail stored; the regression line appears in `/digest`.
- **invariants:**
  - AST import restrictions for `core/idle/`;
  - no idle code path sends Telegram messages (a mock bot asserts zero sends during idle runs);
  - welfare, OOC and canned rows are excluded from all idle inputs.
- **backup:**
  - an encrypt-then-upload stream test with a fake S3 and a test age keypair;
  - plaintext never written to disk (assert no temp file contains a dump header);
  - pruning keeps the right set;
  - failure is recorded.
- **delete:** purges the backup objects in a fake S3.
- **retention:** each sweep; messages deleted only when their scene is summarized.
- **liveness:** `/readyz` fails when the heartbeat is stale.
- **coverage:** export/delete include the new tables.

---

## 13. Milestones (each deployable)

- **6a. Idle framework:** tables, idle gate, planner, preemption, `idle_run`/`idle_change`, undo engine, `/digest` skeleton, `backfill`.
- **6b. Memory and notebook:** `consolidate` and `reflect` with undo and the digest buttons.
- **6c. Quality:** `prebrief` wired into morning, `critique`, `canary`, critique aggregates passed to the review.
- **6d. Research:** `/interests`, idle research on the shared quota.
- **6e. Hardening:** backups, pruning, `/delete` purge, restore runbook and check (first successful restore logged), retention sweeps, `/privacy`, liveness in `/readyz`, CI pip-audit and gitleaks, Dependabot, secrets runbook.

## 14. Acceptance checklist

- [ ] After about 3h of silence, `/digest` shows idle runs. Writing to Anchor mid-run stops new idle work immediately.
- [ ] While paused, or for 24h after a welfare trigger, `/digest` shows only `paused` or `welfare_cooldown` skips.
- [ ] Duplicate-ish facts get merged overnight. **[Отменить]** brings back the originals exactly.
- [ ] `/mind` gains sensible entries from `reflect`; undo removes them.
- [ ] The morning message reflects the pre-brief notes when present, and still arrives when they're absent.
- [ ] `/interests add ref привычки` → the next day, pending cards appear in `/notes` without any push message.
- [ ] Idle spend never exceeds `IDLE_USD_CAP`, and live chat still works when idle spent its full cap.
- [ ] A nightly backup appears in the bucket; `scripts/restore_check.py` restores it locally, and the row counts match.
- [ ] `/delete` removes all rows **and** all backup objects.
- [ ] `/privacy` shows the note. `/readyz` goes red when the heartbeat is stopped.
- [ ] CI runs pip-audit and gitleaks green. All tests pass. Logs and `idle_run.summary` contain no text.
