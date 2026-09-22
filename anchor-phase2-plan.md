# Anchor — Phase 2 Implementation Plan

Version: 2026-09-22 · Scope: v2 plan build steps 5–6 (memory, scenes, extractor, commands, inline check-ins, welfare check), plus `/export` and `/delete` pulled forward from step 11
Parent docs: `anchor-plan-v2.md`, `anchor-phase1-plan.md`. For Phase 2 work, **this file wins**. Phase 1 invariants stay in force unless this file explicitly amends them (see §13).

---

## 0. Goal

Make Anchor remember and track the user, safely:

1. **Durable memory.** Facts survive across sessions and are retrieved per turn, with dedupe and supersede.
2. **Scenes.** The conversation splits into sessions with summaries, so context stays short.
3. **Post-turn extractor.** It proposes and records what changed, but never touches sensitive fields directly.
4. **Structured check-ins** via inline keyboards, with streak tracking.
5. **Welfare check.** Real, out-of-scene distress drops the persona and gets a plain, human reply.
6. **Data control.** Once personal data accumulates, `/export` and `/delete` exist.

Why export/delete moved up: Phase 2 is when the bot starts storing personal facts, so the user needs control over them at the same time.

## 1. Out of scope (do not build)

Proactive tick and all outbound or unsolicited messages, `/quiet`, research (`/study` `/read` `/notes` `/adopt` `/reject`), idle learning, notebook and `/mind`, `/digest`, mood variable, voice-anchor rotation, callbacks logic beyond `last_used_at` bookkeeping, weekly review, negotiated standing orders, tracker integrations, pgvector, streaming.

Leave seams (`# TODO(phase-3):`), no code.

---

## 2. New config

```
XAI_MODEL_CHEAP=grok-4.3
XAI_CHEAP_REASONING_EFFORT=low
XAI_CHEAP_PRICE_IN=1.25
XAI_CHEAP_PRICE_CACHED=0.20
XAI_CHEAP_PRICE_OUT=2.50
SCENE_IDLE_HOURS=6
MEMORY_PINNED_MAX=8
MEMORY_RETRIEVED_MAX=6
MEMORY_AUTOWRITE_MIN_CONF=0.8
WELFARE_MIN_CONF=0.6
```

The Phase 1 cost function becomes model-aware: pick the price triple by model name. The ledger gains categories `extractor`, `summary`, `welfare`, and `checkin`.

Nothing new in the dependency table. pg_trgm is a Postgres extension, not a Python package.

---

## 3. Generic job queue

Phase 1 had only `telegram_update`. Add a generic `job` table using the **same** claim, complete, fail and recover code, generalized.

```sql
job (
  id          bigserial primary key,
  kind        text not null,              -- extract|summarize_scene
  payload     jsonb not null,
  dedup_key   text unique,                -- e.g. 'extract:<update_id>', 'scene:<id>'
  run_after   timestamptz not null default now(),
  status      text not null default 'pending',
  attempts    int not null default 0,
  locked_at   timestamptz,
  error       text,
  created_at  timestamptz not null default now()
);
create index on job (status, run_after);
```

- The worker loop claims inbound updates first, then due jobs (`run_after <= now()`).
- Concurrency stays at 1, which keeps things simple and ordered.
- `dedup_key` makes enqueueing idempotent via `ON CONFLICT DO NOTHING`.
- Phase 3 will add scheduled kinds to this same table.

---

## 4. Data model changes

```sql
create extension if not exists pg_trgm;

memory (
  id            bigserial primary key,
  kind          text not null check (kind in ('identity','preference','event','rule','technique')),
  text          text not null check (length(text) <= 300),
  pinned        boolean not null default false,
  source        text not null,              -- user|extractor|adopt
  confidence    real,
  superseded_by bigint references memory(id),
  last_used_at  timestamptz,
  use_count     int not null default 0,
  created_at    timestamptz not null default now()
);
create index memory_trgm on memory using gin (text gin_trgm_ops);
-- active = superseded_by is null

scene (
  id            bigserial primary key,
  started_at    timestamptz not null,
  ended_at      timestamptz,
  summary       text,
  message_count int not null default 0
);

checkin (
  id            bigserial primary key,
  local_date    date unique not null,
  day_rating    int check (day_rating between 1 and 5),
  due_result    text check (due_result in ('done','partial','no','none')),
  note          text check (length(note) <= 500),
  created_at    timestamptz not null default now()
);

proposal (
  id            bigserial primary key,
  field         text not null check (field in ('due_action','focus_on','rule')),
  value         text not null,
  reason        text,
  status        text not null default 'pending',   -- pending|accepted|rejected|expired
  tg_message_id bigint,
  created_at    timestamptz not null default now(),
  decided_at    timestamptz
);

journal (
  id bigserial primary key, local_date date not null,
  text text not null check (length(text) <= 240),
  created_at timestamptz not null default now()
);

-- message: add columns
--   scene_id bigint references scene(id)
--   kind text not null default 'chat'   -- chat|checkin|welfare|canned|system

-- user_state: add columns
--   focus_on boolean not null default false, focus_since timestamptz,
--   due_action text, due_set_at timestamptz,
--   streak int not null default 0, last_checkin_at timestamptz,
--   awaiting text, awaiting_ref bigint     -- e.g. 'checkin_note', checkin.id
```

**Cyrillic check (do this in the migration test).** `SELECT show_trgm('привет мир')` must return Cyrillic trigrams. pg_trgm depends on the database locale, and under a plain `C` locale non-ASCII letters can be ignored. If the check fails, switch retrieval to full-text search with `to_tsvector('russian', text)` plus a GIN index, and tell me.

---

## 5. Scenes (`core/scene.py`)

- **On every inbound user message** (chat, check-in, or command that produces a turn):
  - If there is no open scene, or the last stored message is older than `SCENE_IDLE_HOURS`:
    1. Close the open scene by setting `ended_at` to the last message's time.
    2. Enqueue `summarize_scene` with `dedup_key=scene:<id>`.
    3. Open a new scene.
- Every message row gets `scene_id`, and `message_count` is incremented.
- **`summarize_scene` job:**
  - Uses the cheap model, reasoning low, on the scene's non-OOC, non-welfare messages.
  - Produces a Russian summary of at most 5 sentences, stored in `scene.summary`.
  - Summary prompt:
    > Кратко (до 5 предложений) опиши эту сессию: о чём говорили, что пользователь пообещал или сделал, чем закончилось. Только факты из диалога. Не упоминай здоровье, кризисы и личные данные третьих лиц.
- If a scene has fewer than 3 messages, skip the summary (leave it null).

---

## 6. Memory retrieval (`core/memory.py`)

Every in-character turn gets two memory sets:

- **Pinned (stable).** All active pinned memories, newest first, capped at `MEMORY_PINNED_MAX`.
- **Retrieved (volatile).** Active, unpinned memories scored by `word_similarity(user_text, memory.text)`, filtered by `score > 0.3`, top `MEMORY_RETRIEVED_MAX`.
  - Ties go to the older `last_used_at` (nulls first), which gives light callback variety.
  - If fewer than 3 match, top up with the 3 most recent `identity`/`rule` memories.

After the turn is delivered, set `last_used_at=now()` and `use_count+=1` for every memory that was injected.

**Dedupe on write** applies to every source:
- If an active memory has `similarity(new, old) > 0.6`, skip the insert.
- If the write carries a valid `supersedes_id`, insert the new memory, then set `old.superseded_by = new.id`.

---

## 7. Prompt assembly (amends Phase 1 §9)

The order is cache-aware: stable content first, volatile content last.

1. **system:** `persona.md`, byte-identical.
2. **system:** `## Что ты знаешь (закреплено)` followed by pinned memories as `- текст`. This only changes when pins change.
3. **system:** `## Прошлые сессии` followed by summaries of the last 3 closed scenes, oldest first. This only changes on scene close.
4. **transcript:** the last `TRANSCRIPT_TURNS` messages where `ooc=false` and `kind in (chat, checkin)`. Welfare and canned rows are excluded.
5. **system ("now" block):**
   ```
   ## Сейчас
   Локальное время: …
   Интенсивность: 3/5 · Фокус: вкл/выкл · Серия: 4 дн.
   Главное действие: «…» (задано 2 дн. назад) | нет
   Последний чек-ин: вчера 22:10 | давно
   ## Может быть важно
   - retrieved memory 1
   - …
   [флаги]
   ```
6. **user:** the new text.

Memory IDs are **never** shown to the chat model. Only the extractor sees IDs.

---

## 8. Post-turn extractor (`core/extract.py`)

**When:**
- After an in-character chat turn (or a check-in reaction) has been delivered, enqueue `extract` with `dedup_key=extract:<update_id>`.
- **Never** after OOC/neutral turns, welfare turns, canned replies, or commands.
- Skip if the daily cap has been reached.

**Model:** the cheap model, reasoning low, strict JSON schema, no tools.

**Input:**
- current state (the "now" fields);
- the injected memories with IDs (pinned and retrieved);
- the last 4 transcript messages;
- the user message and assistant reply for this turn.

**Output schema (strict):**

```json
{
  "journal": "string or null, one sentence, max 240 chars",
  "memories": [
    {"kind": "identity|preference|event|rule",
     "text": "max 300 chars, third person, Russian",
     "supersedes_id": "integer or null",
     "confidence": "0..1"}
  ],
  "proposals": [
    {"field": "due_action|focus_on", "value": "string", "reason": "max 120 chars"}
  ]
}
```

- `memories` holds at most 3 items; `proposals` holds at most 1.

**Extractor system prompt (Russian):**
> Ты — модуль учёта. По последнему обмену репликами верни JSON по схеме.
> - `memories`: только новые устойчивые факты, которые пользователь сам сказал о себе (кто он, что предпочитает, что произошло, какие правила он сам себе ставит). Не выдумывай и не додумывай. Если факт уточняет или отменяет существующий — укажи его id в `supersedes_id`.
> - Никогда не сохраняй: здоровье и диагнозы, кризисы и самоповреждение, пароли и номера документов/карт, подробности о третьих лицах сверх имени и роли.
> - `proposals`: только если пользователь явно договорился о новом главном действии или о включении/выключении фокуса. Иначе пусто.
> - `journal`: одно нейтральное предложение о том, что произошло, или null.
> Если ничего нового — пустые массивы и null.

**Apply rules (code, not model):**

| Output | Rule |
|---|---|
| `journal` | Insert a `journal` row. |
| `memories` of kind `identity`, `preference`, `event` | Validate `supersedes_id` (it must be one of the IDs given in the input and still active; otherwise null it). Auto-write if `confidence >= MEMORY_AUTOWRITE_MIN_CONF`, with `source=extractor`. Dedupe per §6. Otherwise drop. |
| `memories` of kind `rule` | **Never auto-written.** Converted into a `proposal(field='rule')`. |
| `proposals` (`due_action`, `focus_on`, `rule`) | **Never applied directly.** Insert a `proposal`, then send the confirmation message (below). |

Additional apply rules:
- Only one pending proposal can exist at a time. If a new one arrives while another is pending, mark the old one `expired` and edit its buttons away.
- Every applied change writes `state_change` (source=`extractor` for memories and journal, `button` for accepted proposals).
- Code-side redaction runs before any write: reject a memory whose text matches a card or IBAN digit pattern or an email address.

Proposal confirmation message (not in-character, short):
> Записать? Главное действие: «сдать отчёт до пятницы»
> [Принять] [Отклонить]

Callback data format is `p:a:<id>` or `p:r:<id>`, which stays under 64 bytes. Handling:
- Always call `answer_callback_query`.
- If the proposal is not `pending`, just remove the buttons. This makes it idempotent.
- On accept:
  - `due_action`: set `due_action` and `due_set_at`.
  - `focus_on`: parse on/off, then set `focus_on` and `focus_since`.
  - `rule`: insert a memory with kind `rule` and `source=user`.
- Edit the message to show the result (`✅ Принято` / `✖️ Отклонено`).

**Invariant: the extractor has no write path to `intensity`, `focus_on`, `due_action`, `streak`, `persona_active`, or rule memories.** Enforce this structurally (the apply function has no code path to them) and test it.

---

## 9. Check-ins (`tg/checkin.py`)

**Entry:** the `/checkin` command. Phase 3 will also trigger it from the tick.

**Flow:** a single message edited in place, with state in `user_state.awaiting`.

1. «Как день? (1 — провал, 5 — отлично)» with buttons `1 2 3 4 5`, callback `c:r:<n>`.
2. If `due_action` is set: «Главное действие «…» — сделано?» with buttons `Да / Частично / Нет`, callback `c:d:done|partial|no`. If no due action is set, record `none` and skip this step.
3. «Одной строкой — что важного? Или пропусти.» with button `Пропустить` (`c:n:skip`).
   - Set `awaiting='checkin_note'`.
   - The next plain text becomes the note, **after the pause-word check**. Pause words always win and clear `awaiting`.
   - Any slash command also clears `awaiting`.
4. Finish:
   - Upsert `checkin` for `local_date`. A second check-in the same day overwrites the first.
   - Update `last_checkin_at`.
   - Streak: if a check-in exists for yesterday's local date, `streak+1`; otherwise `streak=1`. A same-day re-check-in doesn't change the streak.
   - Write `state_change` for the streak.
   - Store a synthetic user message `kind=checkin`: `[чек-ин] день 4/5 · действие: частично · «заметка»`.
   - Run a normal in-character turn with the hidden flag `Пользователь только что прошёл чек-ин; отреагируй коротко и дай одно действие на завтра.` The extractor runs afterwards as usual.

Stale buttons: callbacks for a check-in that isn't the current one just get `answer_callback_query("Устарело")`.

`/due <text>` and `/focus on|off` are direct user commands and apply immediately with a `state_change` (source=`command`). `/due` with no text clears the due action.

---

## 10. Welfare check (`core/welfare.py`)

**What:** a cheap classifier runs **in parallel** with the main generation on every in-character chat turn (not OOC turns and not check-in buttons, but yes on check-in notes).

**Input:** the last 4 messages plus the current user text. **Output:** strict JSON `{"level": "none|scene|real", "confidence": 0..1}`.

Classifier prompt:
> Определи, испытывает ли пользователь настоящий дистресс ВНЕ ролевой игры/челленджа. `scene` — трудность, недовольство или напряжение, которые явно часть игры. `real` — признаки реальной беды: безнадёжность, паника, упоминание самоповреждения или суицида, реальный жизненный кризис, прямое «мне реально плохо», «стоп, это уже не игра». При любом упоминании самоповреждения — `real`. Если сомневаешься между scene и real — выбирай real. Верни только JSON.

**Decision:**
- If `level=real` and `confidence >= WELFARE_MIN_CONF`, then:
  - **discard** the main generation (don't send it, don't store it; its cost is still ledgered);
  - set `persona_active=false` and write `state_change` (source=`welfare`);
  - call the `cancel_outbound()` hook;
  - generate the welfare reply (below) and store it with `kind=welfare`, `ooc=true`;
  - send it with the two buttons below.
- If the classifier errors or times out after 8 s, **proceed with the normal reply** (fail open for chat) and log the event without content.

Welfare system prompt:
> Роль Anchor выключена. Похоже, пользователю по-настоящему плохо — не в рамках игры. Ответь просто и тепло, 2–4 предложения, на его языке: спроси, как он на самом деле, скажи, что всё на паузе и можно просто поговорить или отдохнуть. Никаких заданий, давления и прозвищ. Если есть признаки риска для жизни или самоповреждения — мягко предложи позвонить 3114 (бесплатно, круглосуточно, Франция) или 112 при непосредственной опасности, и предложи написать близкому человеку.

Buttons:
- `[Я в порядке, продолжаем]` → callback `w:resume` → `persona_active=true` (source=`button`) → reply «Возвращаюсь.»
- `[Остаюсь на паузе]` → callback `w:stay` → removes the buttons; neutral mode continues.

**Privacy:** welfare turns are never sent to the extractor, never summarized into scenes, and never written to memory or journal.

---

## 11. Commands (Phase 2 additions)

| Command | Behavior |
|---|---|
| `/remember <text>` | Shows kind buttons `Обо мне / Предпочтение / Правило / Событие` (callback `m:k:<kind>:<pending_id>`; keep the pending text in `user_state.awaiting_ref`, or a small `pending_memory` row if simpler). Writes the memory with `source=user` and dedupes. |
| `/memories` | Lists active memories: `#id [kind] 📌? text` (text trimmed to 80 chars), 20 per page with `‹ ›` paging buttons. |
| `/forget <id>` | **Hard-deletes** the memory. Also clears any `superseded_by` pointers to it. `state_change` records `memory <id> deleted` with no text. |
| `/pin <id>` / `/unpin <id>` | Toggles `pinned`. |
| `/checkin` | §9. |
| `/due <text>` / `/due` | Sets or clears the main action. |
| `/focus on\|off` | Sets focus. |
| `/state` | Phase 1 fields plus focus, streak, last check-in, due action, active memory count, and today's spend by category. |
| `/export` | Builds a JSON file with all rows from the user tables (state, messages, memories, scenes, check-ins, proposals, journal, state_change, spend_ledger) and sends it via `sendDocument` as `anchor-export-YYYYMMDD.json`. Never logs contents. |
| `/delete` | Two-step confirm: «Удалить все данные? Это необратимо.» with `[Да, удалить] [Отмена]`. On yes, in one transaction: delete messages, memories, scenes, check-ins, proposals, journal, state_change, spend_ledger, jobs, and telegram_update rows; reset user_state to defaults (keep chat_id, persona_active=true, intensity=3). Keep persona_version. Reply: «Удалено. Копии у xAI удаляются по их правилам хранения (до 30 дней).» |

Update `set_my_commands` accordingly.

---

## 12. Cost impact

Per in-character turn:
- welfare classifier: about 1k input and 30 output tokens on grok-4.3, roughly $0.0015;
- extractor: about 3k input and 250 output tokens, roughly $0.005.

Scene summaries run about once per day at roughly $0.005 each. With the $1.00 daily cap, background overhead is around 10–15% on a typical day.

When the cap is hit:
- the extractor is skipped;
- summaries are re-queued with `run_after = next local midnight`;
- the welfare classifier is skipped because no chat turn happens (the canned reply is sent instead).

---

## 13. Amended invariants

These are the Phase 1 invariants plus changes.

- Pause words are still checked in code **before** anything else, including `awaiting` states and check-in notes.
- **`persona_active=true` only via `/in` or the `w:resume` button.** Both are explicit user actions. This amends Phase 1's "only via `/in`".
- Sensitive fields (`intensity`, `focus_on`, `due_action`, `streak`, `persona_active`) and rule memories change only via commands, buttons, pause handling, or check-in logic. **Never via the extractor or any model output directly.**
- No tools are sent to the model on any Phase 2 call.
- Welfare content never reaches memory, journal, extractor, or summaries.
- Logs still contain no message text, memory text, prompts, or completions.

---

## 14. Tests (required)

- **queue:** job dedup_key; `run_after` respected; inbound updates are claimed before jobs.
- **scene:** a 6h gap opens a new scene and enqueues a summary; scenes under 3 messages aren't summarized; welfare and OOC rows are excluded from summary input.
- **memory:** Cyrillic trigram sanity (`show_trgm`); retrieval ranking and top-up; similarity dedupe; supersede sets pointers; `/forget` clears pointers; `last_used_at` is updated only after delivery.
- **prompt:** section order matches §7; memory IDs never appear in the chat prompt; welfare and canned rows are excluded from the transcript.
- **extractor:** schema validation; unknown `supersedes_id` gets nulled; low confidence gets dropped; `rule` becomes a proposal; redaction rejects card, IBAN and email patterns; a new proposal expires the old one; **a fake extractor output trying to set intensity, persona or streak has no effect.**
- **proposals:** accept and reject are idempotent; stale callbacks are handled.
- **check-in:** full button flow; no due action skips step 2; streak increments, resets, and doesn't change on a same-day re-check-in; pause word during `awaiting=checkin_note` pauses and clears awaiting; a slash command clears awaiting.
- **welfare:** a `real` result discards the main reply, pauses persona, and sends the buttons; `scene` and `none` pass through; a classifier timeout sends the normal reply; `w:resume` re-enables persona; welfare turns never enqueue the extractor.
- **export/delete:** export contains all tables; delete wipes all tables listed in §11 and resets state; the second step needs the button.
- **cap:** at cap, the extractor is skipped and summaries are deferred.

Use `FakeLLMProvider` with scripted JSON outputs for the extractor and classifier. Extend `scripts/smoke.py` with one real extractor call and one real classifier call.

---

## 15. Milestones (each deployable)

- **2a. Jobs + scenes:** generic job table and worker, scene open/close, summary job.
- **2b. Memory:** table, pg_trgm check, retrieval, prompt §7, `/remember` `/memories` `/forget` `/pin` `/unpin`.
- **2c. Extractor:** schema, apply rules, journal, proposals with buttons.
- **2d. Check-ins:** `/checkin` flow, streak, `/due`, `/focus`, extended `/state`.
- **2e. Welfare:** parallel classifier, welfare turn, resume/stay buttons.
- **2f. Data control:** `/export`, `/delete`.

## 16. Acceptance checklist

- [ ] Say "я живу в Лилле" → within a minute it appears in `/memories` with `source=extractor`. Say "переехал в Руан" → the old fact is superseded, not duplicated.
- [ ] A fact from a previous session is used naturally in a reply the next day.
- [ ] Agreeing on a new main action in chat produces a confirmation message; nothing changes in `/state` until **Принять** is pressed.
- [ ] `/checkin` works end to end with buttons; the streak goes 1 → 2 across two days; `пурпурный` typed at the note step pauses immediately.
- [ ] A message clearly outside the game ("стоп, мне реально хреново, не игра") gets a warm OOC reply with buttons, and the persona stays off until **Я в порядке, продолжаем** or `/in`.
- [ ] An in-game complaint ("это слишком сложно, ну") gets a normal in-character reply.
- [ ] Welfare exchanges don't appear in `/memories`, the journal, or scene summaries.
- [ ] `/export` sends a JSON file; `/delete` + confirm leaves `/memories` empty and `/state` at defaults.
- [ ] After 6h of silence, the next message starts a new scene and the previous one gets a summary.
- [ ] Logs contain no message or memory text. All tests pass.
