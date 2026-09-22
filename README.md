# Anchor — Milestone 2b

A private, single-user Telegram bot.

Phase 1 proved the intake path is safe and exactly-once: a
secret-verified webhook that accepts updates only from
`ALLOWED_CHAT_ID` in a private chat, stores them durably with dedup,
and a single-concurrency worker that drains the queue into aiogram,
runs the persona turn against OpenRouter (model
`thedrummer/cydonia-24b-v4.1`), and replies — with pause-word/`/out`
handling and a daily USD spend cap in place.

Milestone 2a adds the machinery Phase 2 is built on:

- **A generic job queue** (`job` table, `app/db/jobs.py`). Phase 1's
  inbound-update queue and this one are the same claim/complete/fail/
  recover code, parameterized (`app/db/queue.py`). The worker claims
  inbound updates first and only then a due job, so background work
  never delays a reply.
- **Scenes** (`scene` table, `app/core/scene.py`). Silence longer than
  `SCENE_IDLE_HOURS` closes the open scene and queues a cheap-model
  summary of it; every `message` row now carries `scene_id` and a
  `kind` (`chat|checkin|welfare|canned|system`).
- **A background LLM provider**, sharing one HTTP client with the chat
  provider. It runs the same model today (`LLM_MODEL_CHEAP`) but has
  its own price triple, so `compute_cost` is genuinely model-aware and
  the background model can be swapped with one env var.

Milestone 2b makes it remember:

- **Durable memory** (`memory` table, `app/core/memory.py`). Facts
  survive across sessions, are retrieved per turn by trigram similarity,
  deduped on write, and superseded rather than duplicated when a fact
  changes.
- **Prompt assembly per plan §7**: persona → pinned memories → last 3
  scene summaries → transcript → the "now" block with the retrieved
  memories → the new message. Memory IDs never reach the chat model —
  `build_messages` is handed strings, not rows, so that cannot be
  violated by accident.
- **`/remember` `/memories` `/forget` `/pin` `/unpin`**, the bot's first
  inline keyboards.

See `app/config.py` and inline `# TODO(phase-N):` comments for what is
deliberately deferred.

### Retrieval, and one deviation from the plan

`word_similarity(a, b)` is asymmetric: the whole of `a` must be matched
by some continuous extent of `b`. The plan specifies
`word_similarity(user_text, memory.text)`, which makes the user's whole
message the needle and therefore scores *lower the more they type* —
long, context-rich messages would retrieve nothing. Measured against a
memory `"пользователь живёт в Лилле"`:

| user text | plan order | flipped |
|---|---|---|
| `Лилль` | 0.667 | 0.179 |
| `я сегодня думал про Лилль` | 0.154 | 0.194 |
| `слушай, я сегодня ехал домой … Лилль` | 0.075 | 0.194 |

So the arguments are flipped, and the cutoff moves with them (0.15, not
the plan's 0.3, which was calibrated for the un-flipped orientation).
Both numbers live in `app/core/memory.py` with the measurements that
justify them; `tests/test_memory.py` asserts ranking and separation, not
the floats.

## Local setup

```bash
uv sync
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN, TELEGRAM_SECRET_TOKEN, ALLOWED_CHAT_ID,
# DATABASE_URL (e.g. postgresql://anchor:anchor@127.0.0.1:5432/anchor_dev)
uv run alembic upgrade head
```

## Run locally (polling)

Set `MODE=polling` in `.env`, then:

```bash
uv run python -m app.main
```

Polling mode calls `delete_webhook` on start, then long-polls
`getUpdates`, feeding the same allow-list/enqueue path the webhook route
uses. It is for local development only; production runs `MODE=webhook`.

## Tests

```bash
sudo scripts/setup-postgres.sh   # once, if you have no PostgreSQL 18
export ANCHOR_ADMIN_DATABASE_URL="postgresql://anchor:anchor@127.0.0.1:5433/postgres"
uv run pytest
```

Tests use an already-running PostgreSQL cluster (`pg_isready`),
creating and dropping a throwaway `anchor_test_<rand>` database per
session. Set `TEST_DATABASE_URL` to point tests at a specific database
instead, or `ANCHOR_ADMIN_DATABASE_URL` to choose which cluster the
throwaway database is created on. If no cluster is reachable,
DB-dependent tests are skipped.

**Version and locale.** Production runs PostgreSQL 18 (Railway's
`postgres-ssl:18`), so the fixture prefers an 18 cluster and warns on
anything older; `scripts/setup-postgres.sh` provisions one. The test
database is created with an explicit `LOCALE 'C.UTF-8'` rather than
inheriting `template1`'s, because under a plain `C` locale `pg_trgm`
silently stops seeing Cyrillic — `show_trgm('привет мир')` returns zero
trigrams and every `similarity()` is `0`, with no error anywhere. That
would make memory retrieval quietly return nothing, so the suite must
never be able to pass under a locale production does not use.

As of 2b the 2b migration asserts the same thing and **aborts** if it
fails, naming the cause and the fallback: a database in that state can
never carry the schema that depends on trigram search. A database's
locale is fixed at `CREATE DATABASE`, so the remedy is a new database,
not a redeploy.

## Smoke test (real API calls)

```bash
uv run python scripts/smoke.py
```

The one place allowed to touch the network. Prints, on live data: which
provider served the call, whether `LLM_DATA_COLLECTION` routed, token
counts, OpenRouter's reported cost vs. ours, the web-search fee delta,
a real scene summary through the background model, and a strict
`json_schema` probe. Read the `STRUCTURED OUTPUTS:` verdict line before
starting milestone 2c — the extractor and welfare classifier depend on
it.

## Deploy (Railway)

1. BotFather: create the bot, disable joining groups (`/setjoingroups` ->
   Disable).
2. Railway: new project from this repo, add the Postgres plugin, set the
   env vars from `.env.example` (`MODE=webhook`).
3. Start command: `alembic upgrade head && python -m app.main`.
   Healthcheck path: `/healthz`.
4. Generate a public domain, set `PUBLIC_URL` to it, redeploy. The app
   sets its own webhook on boot (`set_webhook` in `app/main.py`).
5. Keep exactly one replica — the worker assumes single-consumer
   ordering.

## Privacy

No message text, prompt, completion, or raw update payload is ever
logged — only IDs, counts, and latency (see `app/log.py`). Secrets live
only in environment variables; `.env` is gitignored and must never be
committed.
