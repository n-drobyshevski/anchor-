# Anchor — Milestone 1a

A private, single-user Telegram bot. Milestone 1a proves the intake path
is safe and exactly-once: a secret-verified webhook that accepts updates
only from `ALLOWED_CHAT_ID` in a private chat, stores them durably with
dedup, and a single-concurrency worker that drains the queue into
aiogram and echoes the text back. No LLM, no persona yet — see
`app/config.py` and inline `# TODO(phase-N):` comments for what is
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
uv run pytest
```

Tests use the already-running Postgres 16 cluster (`pg_isready`),
creating and dropping a throwaway `anchor_test_<rand>` database per
session. Set `TEST_DATABASE_URL` to point tests at a specific database
instead. If no PG16 binaries are found, DB-dependent tests are skipped.

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
