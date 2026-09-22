# Anchor — Phase 1 Implementation Plan

Version: 2026-09-21 · Scope: v2 plan build steps 1–4 (+ pause words and spend cap, pulled forward)
Parent doc: `anchor-plan-v2.md`. Where the two differ, **this file wins for Phase 1**.

---

## 0. Goal

A deployed, private Telegram bot on Railway that:

1. accepts updates only from `ALLOWED_CHAT_ID`, via a secret-verified webhook;
2. stores every inbound update durably and processes it exactly once;
3. replies in character (Russian, from `persona.md`) using xAI Grok, with the recent transcript as context;
4. drops persona instantly on pause words, in code, before any model call;
5. never spends more than `DAILY_USD_CAP` per local day.

Why pause words and the spend cap are in Phase 1: the moment the persona speaks, the off-switch must exist, and the moment the API key is live, the wallet guard must exist.

## 1. Out of scope (do not build)

Memory table and retrieval, post-turn extractor, scenes and summaries, notebook, inline check-ins, proactive tick and outbound messages, research, idle learning, welfare classifier, tracker integrations, `/remember` `/quiet` `/study` `/read` `/notes` `/adopt` `/reject` `/mind` `/digest` `/export`, streaming, pgvector. Leave clean seams for them; write no code for them.

---

## 2. Stack (decided)

| Purpose | Library | Links |
|---|---|---|
| Telegram | aiogram 3.x | [PyPI](https://pypi.org/project/aiogram/) · [GitHub](https://github.com/aiogram/aiogram) |
| HTTP server | aiohttp (aiogram's native integration) | [PyPI](https://pypi.org/project/aiohttp/) · [GitHub](https://github.com/aio-libs/aiohttp) |
| DB ORM | SQLAlchemy 2.x async | [PyPI](https://pypi.org/project/SQLAlchemy/) · [GitHub](https://github.com/sqlalchemy/sqlalchemy) |
| DB driver | asyncpg | [PyPI](https://pypi.org/project/asyncpg/) · [GitHub](https://github.com/MagicStack/asyncpg) |
| Migrations | Alembic | [PyPI](https://pypi.org/project/alembic/) · [GitHub](https://github.com/sqlalchemy/alembic) |
| Config | pydantic-settings | [PyPI](https://pypi.org/project/pydantic-settings/) · [GitHub](https://github.com/pydantic/pydantic-settings) |
| LLM client | openai SDK pointed at `https://api.x.ai/v1` | [PyPI](https://pypi.org/project/openai/) · [GitHub](https://github.com/openai/openai-python) |
| Tests | pytest + pytest-asyncio | [PyPI](https://pypi.org/project/pytest-asyncio/) · [GitHub](https://github.com/pytest-dev/pytest-asyncio) |
| Packaging | uv | [PyPI](https://pypi.org/project/uv/) · [GitHub](https://github.com/astral-sh/uv) |

Python 3.12. Pin exact versions in `uv.lock`.

**Queue decision for Phase 1:** a hand-rolled inbound queue on the `telegram_update` table with `FOR UPDATE SKIP LOCKED` (about 60 lines). No procrastinate yet; revisit when scheduled jobs arrive in Phase 3.

**LLM client decision:** the openai SDK with `base_url="https://api.x.ai/v1"`, because it supports `extra_headers` (needed for `x-grok-conv-id`) and both Responses and Chat Completions. Everything sits behind `LLMProvider`, so switching to `xai-sdk` later is a one-file change. **Verify against docs.x.ai before coding:** the Responses endpoint shape, the `reasoning` parameter shape, the usage field names (cached and reasoning tokens), and `store=false`. If Responses via the openai SDK misbehaves, use Chat Completions; the interface hides it.

---

## 3. Repo layout

```
anchor/
  pyproject.toml
  alembic.ini
  migrations/                 # alembic env + versions
  persona/persona.md
  app/
    main.py                   # entry: MODE=webhook|polling
    config.py                 # pydantic-settings
    log.py                    # structured logging, content redaction
    db/
      models.py
      session.py
      queue.py                # enqueue / claim / complete / fail / recover
    tg/
      webhook.py              # aiohttp route: verify, filter, enqueue, 200
      polling.py              # dev: getUpdates loop -> same enqueue path
      router.py               # aiogram Dispatcher + handlers
      send.py                 # split + send + typing
    worker.py                 # claim loop -> dp.feed_update
    core/
      pause.py                # normalize + match pause words
      prompt.py               # message assembly
      spend.py                # cost calc + daily cap
      split.py                # 4096-char splitter
      turn.py                 # one chat turn, idempotent
      state.py                # UserState read/update + StateChange audit
    llm/
      provider.py             # Protocol + dataclasses
      xai.py                  # implementation
  tests/
```

---

## 4. Configuration

```
MODE=webhook                    # polling for local dev
TELEGRAM_BOT_TOKEN=
TELEGRAM_SECRET_TOKEN=          # 1–256 chars, A-Z a-z 0-9 _ -
ALLOWED_CHAT_ID=
PUBLIC_URL=https://<service>.up.railway.app
XAI_API_KEY=
XAI_MODEL=grok-4.7
XAI_REASONING_EFFORT=medium
XAI_PRICE_IN=2.00               # USD per 1M input tokens
XAI_PRICE_CACHED=0.50           # USD per 1M cached input tokens
XAI_PRICE_OUT=6.00              # USD per 1M output tokens (reasoning billed as output)
DAILY_USD_CAP=1.00
TZ_DEFAULT=Europe/Paris
TRANSCRIPT_TURNS=30
DATABASE_URL=                   # Railway gives postgresql:// -> rewrite to postgresql+asyncpg://
LOG_LEVEL=INFO
PORT=                           # set by Railway; bind 0.0.0.0:$PORT
```

Prices are config, not constants. Re-verify on every model change.

---

## 5. Data model (Phase 1 tables only)

```sql
-- inbound queue + dedup in one table
telegram_update (
  update_id     bigint primary key,          -- dedup: insert ... on conflict do nothing
  payload       jsonb not null,
  status        text not null default 'pending',   -- pending|processing|done|failed
  attempts      int  not null default 0,
  locked_at     timestamptz,
  error         text,
  created_at    timestamptz not null default now()
);
create index on telegram_update (status, update_id);

message (
  id              bigserial primary key,
  role            text not null,              -- user|assistant
  content         text not null,
  ooc             boolean not null default false,
  update_id       bigint references telegram_update(update_id),
  reply_to_update bigint unique,              -- assistant rows: idempotency key
  sent_at         timestamptz,                -- assistant rows: null until delivered
  model           text,
  tokens_in       int, tokens_cached int, tokens_out int,
  usd_cost        numeric(10,6),
  created_at      timestamptz not null default now()
);

user_state (                                  -- exactly one row
  id              int primary key default 1 check (id = 1),
  chat_id         bigint not null,
  persona_active  boolean not null default true,
  intensity       int not null default 3 check (intensity between 1 and 5),
  timezone        text not null default 'Europe/Paris',
  updated_at      timestamptz not null default now()
);

state_change (                                -- audit log
  id bigserial primary key, field text, old_value text, new_value text,
  source text,                                -- command|pause|system
  created_at timestamptz not null default now()
);

persona_version (
  id bigserial primary key, sha256 text unique not null,
  body text not null, created_at timestamptz not null default now()
);

spend_ledger (
  id bigserial primary key, ts timestamptz not null default now(),
  local_date date not null,                   -- in user_state.timezone
  category text not null,                     -- chat|ooc
  model text, tokens_in int, tokens_cached int, tokens_out int,
  usd_cost numeric(10,6) not null
);
create index on spend_ledger (local_date);
```

On startup: `alembic upgrade head`, then upsert `user_state` with `ALLOWED_CHAT_ID`, then hash `persona.md` and insert a `persona_version` row if the hash is new.

---

## 6. Request flow

### 6.1 Webhook (`POST /telegram/webhook`)

1. Compare `X-Telegram-Bot-Api-Secret-Token` with `hmac.compare_digest`. On mismatch return 403.
2. Parse JSON. Extract `chat.id` and `chat.type` from `message` or `callback_query.message`.
3. If `chat.type != "private"` or `chat.id != ALLOWED_CHAT_ID`, return 200 and **store nothing**.
4. `INSERT ... ON CONFLICT (update_id) DO NOTHING`.
5. Return 200. No LLM, no Telegram calls, nothing slow.

Also add `GET /healthz` (always 200) and `GET /readyz` (checks the DB).

On startup in webhook mode: `set_webhook(url=PUBLIC_URL+"/telegram/webhook", secret_token=..., allowed_updates=["message","callback_query"])`.

### 6.2 Polling (dev)

`delete_webhook`, then a `getUpdates` loop that runs the **same** filter and enqueue function. One code path for both modes.

### 6.3 Worker (runs in the same process as a background task)

- Concurrency is **1**, which preserves message order for a single user.
- Claim: `SELECT ... WHERE status='pending' ORDER BY update_id LIMIT 1 FOR UPDATE SKIP LOCKED`, then set status to `processing`, set `locked_at`, and increment `attempts`.
- Run `dp.feed_update(bot, Update.model_validate(payload))`.
- On success, set `done`. On exception, set `pending` with the error recorded; after 3 attempts set `failed` and log the update_id only.
- Recovery: every 60 s, reset rows stuck in `processing` for more than 5 min back to `pending`.
- Idle poll interval: 0.5 s. `LISTEN/NOTIFY` is optional.

### 6.4 Handlers (`router.py`)

Order matters.

1. Commands: `/start`, `/out`, `/in`, `/state`.
2. Text: `pause.match(text)`, then either the pause path or `turn.run()`.
3. Anything else (stickers, photos, voice): short fixed reply "Пока только текст." with no LLM call.

---

## 7. Pause words (`core/pause.py`) — code, never the model

**Normalize:** lowercase, `ё→е`, replace every non-letter with a space, split into tokens.

**Match** on any standalone token:

| Level | Rule | Examples |
|---|---|---|
| HARD | token starts with `пурпурн`, or its first 7 chars are within Levenshtein distance 1 of `пурпурн` | пурпурный, Пурпурный!!, пурпурнй |
| HARD | token starts with `красн` (exact prefix only; fuzzy would catch `красивый`) | красный, КРАСНЫЙ. |
| SOFT | token starts with `желт` (exact prefix) | жёлтый, желтый |

`/out` is HARD. If a message contains both HARD and SOFT, HARD wins. False positives are acceptable because they fail safe.

**HARD path:**
- Set `persona_active=false` and write a `state_change` row (source=`pause`).
- Call the `cancel_outbound()` hook (a no-op stub in Phase 1; wire it in Phase 3).
- Send the fixed text below. **No LLM call.**

  > Ок, выхожу из роли. Всё на паузе, никаких сообщений от меня. Вернуться — /in.

**SOFT path:**
- Set `intensity = max(1, intensity-1)` and write a `state_change` row.
- Run a normal turn with the hidden flag `Пользователь сказал «жёлтый»: снизь интенсивность прямо сейчас, мягче, без давления.`
- If already at 1, still run the turn with the flag.

**`/in`:** set `persona_active=true` (intensity is not restored) and send the fixed text "Возвращаюсь." The bot **never** sets `persona_active=true` any other way.

**While `persona_active=false`:** plain text goes to **neutral mode**. That means a minimal system prompt, a context of only the last 10 `ooc=true` messages, the reply stored as `ooc=true`, and spend category `ooc`. No persona, no nicknames.

Neutral system prompt:
> Ты нейтральный ассистент. Роль Anchor сейчас выключена. Отвечай спокойно и по делу, на языке пользователя. Не возвращайся в роль; если спросят как вернуться — подскажи команду /in.

---

## 8. Chat turn (`core/turn.py`) — idempotent

1. Store the user message (`role=user`, `update_id`) if it is not already stored for this update.
2. If an assistant row with `reply_to_update = update_id` exists:
   - If `sent_at` is null, **resend it** (crash between generate and send).
   - Otherwise do nothing.
   - Return in both cases. Never regenerate.
3. **Spend check:** if today's `sum(usd_cost)` in the local date is at least `DAILY_USD_CAP`:
   - Send the canned text "На сегодня всё, лимит. Продолжим завтра."
   - Store it as an assistant row with cost 0 and return.
   - The check happens *before* the call. One call may overshoot the cap slightly, which is acceptable.
4. Send `typing` and refresh it every 4 s while waiting.
5. Assemble the prompt (§9) and call the LLM. Retry at most 2 times on 429/5xx with backoff, honoring `Retry-After`. On final failure send "Связь с моделью упала, попробуй чуть позже." and **do not** mark it as an assistant message, so the user can simply resend.
6. Compute cost (§10). Insert the assistant row (`reply_to_update`, usage, cost) and the `spend_ledger` row **in one transaction**.
7. Split (§11), send the chunks, then set `sent_at`.

---

## 9. Prompt assembly (`core/prompt.py`)

The stable prefix comes first so the prompt cache hits. Volatile content goes last.

1. **system:** `persona.md` body, byte-identical every call.
2. **transcript:** the last `TRANSCRIPT_TURNS` messages with `ooc=false`, oldest first, as `user`/`assistant`.
3. **system ("now" block)**, rebuilt each turn:
   ```
   ## Сейчас
   Локальное время: 2026-09-21 21:40 (Europe/Paris), понедельник
   Интенсивность: 3/5
   [флаги, напр. «жёлтый» из §7]
   ```
4. **user:** the new text.

Request options:
- header `x-grok-conv-id: anchor-main`, constant per conversation;
- `store=false`, because we keep our own transcript;
- reasoning effort from config;
- no tools, ever, on this path.

Append only. Never edit historic messages.

---

## 10. Cost (`core/spend.py`)

```
uncached = tokens_in - tokens_cached
usd = (uncached*XAI_PRICE_IN + tokens_cached*XAI_PRICE_CACHED + tokens_out*XAI_PRICE_OUT) / 1e6
```

- `tokens_out` must include reasoning tokens. Verify the exact usage field names in xAI's response. If xAI returns a cost field, store it and prefer it over the computed value.
- `local_date` is computed in `user_state.timezone`.

---

## 11. Splitting and sending (`core/split.py`, `tg/send.py`)

- Limit 4096 characters; use 4000 for safety.
- Split priority: paragraph break, then sentence end (`. ! ? …`), then whitespace, then hard cut.
- Send plain text in Phase 1 (no `parse_mode`), which avoids entity-parse failures.

---

## 12. Commands

| Command | Behavior |
|---|---|
| `/start` | Fixed intro (2 lines, Russian); mentions `/out` and the pause word. |
| `/out` | HARD pause (§7). |
| `/in` | Resume persona (§7). |
| `/state` | Plain text: persona on/off, intensity, local time, today's spend and cap, model. |

Register the commands with `set_my_commands` on startup.

---

## 13. `persona/persona.md` — starter skeleton

The user edits this file. The code only loads and hashes it.

```markdown
# Anchor

Ты — Anchor. Отвечаешь только по-русски.

## Голос
- Коротко, твёрдо, чуть суховато. 3–6 предложений.
- В каждом ответе ровно одно следующее действие.
- Обращения: {{rotate from config later; пока — без прозвищ}}

## Границы
- Никаких медицинских, юридических советов и советов о необратимых изменениях тела.
- Люди, которых упоминает пользователь, — только фон. Не говори от их имени и не утверждай, что они на что-то согласились.
- Не выдумывай факты о пользователе, которых нет в диалоге.
- Если в контексте есть флаг «жёлтый» — мягче и без давления.

## Примеры реплик (voice anchors)
- «Отчёт за вечер. Три пункта, без романов.»
- «Принято. Завтра то же самое, только до девяти.»
- «Не оправдание. Что сделаешь в ближайший час?»
<!-- добавить до 10–15 -->
```

---

## 14. Logging and privacy

- **Never log message text or payloads.** Log update_id, event names, latency, token counts, and cost.
- A redaction filter in `log.py` drops any `text`, `content`, or `payload` keys.
- Secrets come from env only. `.env` goes in `.gitignore`. Private GitHub repo.

---

## 15. Deploy (Railway)

1. BotFather: create the bot with a neutral username and disable joining groups (`/setjoingroups`, Disable).
2. Railway: create a project from the GitHub repo, add the Postgres plugin, then set the env vars from §4.
3. Start command: `alembic upgrade head && python -m app.main`. Set the healthcheck path to `/healthz`.
4. Generate a public domain, put it in `PUBLIC_URL`, and redeploy. The app sets the webhook on boot.
5. Keep **one replica**.
6. Local dev: `MODE=polling` against a separate dev bot token and a local Postgres (docker).

---

## 16. Tests (required before "done")

- `pause`: every example in §7; negatives (`красивый`, `прекрасно`, `пурпур` alone doesn't match, `желание`); HARD beats SOFT; `/out`.
- `split`: under 4096, exactly 4096, long paragraph without punctuation, Cyrillic.
- `spend`: formula with cached and reasoning tokens; local_date across midnight and DST (last Sunday of October in Paris).
- `queue`: duplicate update_id inserts once; SKIP LOCKED claim; stuck-row recovery; 3-attempt failure.
- `turn`: resend when `sent_at` is null; no regeneration when an assistant row exists; over-cap path makes no LLM call (use a fake provider).
- `webhook`: bad secret returns 403; foreign chat returns 200 with nothing stored; group chat returns 200 with nothing stored.
- `prompt`: persona is first and byte-stable; OOC messages are excluded from the persona transcript; the "now" block is last before the user text.

Use a `FakeLLMProvider` for all tests. Hit the real xAI API only in a manual `scripts/smoke.py`.

---

## 17. Milestones (each deployable)

- **1a. Echo:** webhook + secret + allowlist + dedup queue + worker + echo reply. Deploy and test from the phone.
- **1b. DB and state:** all tables, `user_state`, `/start` `/state`, message storage.
- **1c. Persona turn:** provider + prompt + cost + ledger + split + typing + idempotent turn.
- **1d. Safety:** pause words, `/out` `/in`, neutral mode, daily cap, canned replies.

## 18. Acceptance checklist

- [ ] Messages from another Telegram account get no reply and leave no DB rows.
- [ ] A wrong secret header returns 403.
- [ ] Re-POSTing the same update produces exactly one reply.
- [ ] Killing the process mid-turn and restarting produces one reply, not two and not zero.
- [ ] Russian in-character reply, 3–6 sentences, arrives in under 15 s on a normal day.
- [ ] `Пурпурный!!` returns the fixed OOC text with no LLM call (visible in the ledger); later messages get neutral replies until `/in`.
- [ ] `жёлтый` lowers intensity by 1 (visible in `/state` and `state_change`).
- [ ] With `DAILY_USD_CAP=0.0001`, the canned limit reply appears and no LLM call is made.
- [ ] `/state` shows today's spend matching the sum of `spend_ledger`.
- [ ] Logs contain no message text.
- [ ] All tests pass. `persona.md` edits produce a new `persona_version` row on restart.
