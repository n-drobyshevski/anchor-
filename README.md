# Anchor — Milestone 2a

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

See `app/config.py` and inline `# TODO(phase-N):` comments for what is
deliberately deferred.

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
would make Phase 2's memory retrieval quietly return nothing, so the
suite must never be able to pass under a locale production does not use.

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
