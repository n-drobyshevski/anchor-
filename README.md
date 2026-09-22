# Anchor — Milestone 3b (Anchor speaks first)

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

Milestone 2c closes the loop — the bot now notices things on its own:

- **A post-turn extractor** (`app/core/extract.py`) runs as a background
  job after each delivered in-character turn. It proposes durable facts,
  a one-line journal entry, and at most one change to how the bot pushes.
- **Proposals** (`app/core/proposal.py`). Nothing the model suggests about
  `due_action`, `focus_on`, or a rule is ever applied directly: it lands
  as a `pending` row with `[Принять] [Отклонить]` buttons, and only a
  press applies it.

Milestone 2d adds the daily ritual the rest is built around:

- **`/checkin`** — three steps in one message edited in place: rate the
  day, report the main action, add a one-line note. Finishing runs a
  normal in-character turn with a hidden flag, so Anchor reacts and
  names one action for tomorrow.
- **A streak**, incremented when yesterday has a check-in, reset after a
  gap, and unchanged by a same-day redo.
- **`/due`** and **`/focus`** — direct commands for the two fields 2c's
  proposals can only *suggest*. Typing one expires an outstanding
  proposal for the same field, so a stale `Принять` can't later
  overwrite what you just set.
- **Extended `/state`** with focus, streak, last check-in, due action,
  memory count, and today's spend broken down by category.

Milestone 2e adds the welfare check — the one place where the bot is
meant to stop being Anchor:

- A cheap classifier runs **beside** each in-character generation, so on
  an ordinary turn it adds no latency: the reply was already waiting on
  the slower of the two calls.
- On real, out-of-scene distress the persona reply is **discarded
  unsent** (its cost is still ledgered — the money left whether the
  words did or not), the persona switches off, and a plain, warm
  out-of-character message goes out with two buttons.
- It **fails open**. A classifier that times out, errors, or returns
  something unparseable lets the normal reply through. A check meant to
  catch a case the persona would mishandle must not become a new way
  for the bot to break.
- It **fails toward `real`**. The prompt says to choose `real` when
  torn and `WELFARE_MIN_CONF` is 0.6, because the errors aren't
  symmetric: a false positive is a warm message dismissed with a button,
  a false negative is the persona pushing someone who isn't okay.

Both halves of a welfare exchange are excluded from the extractor,
scene summaries, memory and the journal. The reply is written
`kind='welfare', ooc=True`; the message that *triggered* it was stored
as ordinary chat before anyone knew, so it is retagged the same way
before the turn returns.

`persona_active` can only be set back to true by `/in` or the
«Я в порядке, продолжаем» button (plan §13). Both route through the
same `run_resume`, which is what keeps `tests/test_turn.py`'s
grep-the-whole-source invariant down to a single call site.

Milestone 2f closes Phase 2 with control over everything the previous
four milestones started storing:

- **`/export`** sends a JSON file of §11's nine tables — state, messages,
  memories, scenes, check-ins, proposals, journal, state_change,
  spend_ledger. Money is exported as a string, not a float: `usd_cost` is
  `Numeric(10, 6)`, and a float round-trip would quietly change the
  number in a file whose point is to be accurate. Contents are never
  logged; only byte and row counts.
- **`/delete`** is a two-step confirm, then one `TRUNCATE` over eleven
  tables plus a reset of the singleton state row. `persona_version`
  survives (it's a hash of a file in this repo, not your data), and
  `chat_id` survives so the bot still knows who it's talking to.

The `[Да, удалить]` button **expires after five minutes**. §11 specifies
a two-step confirm and no expiry; left literal, a button in scrollback
wipes everything irreversibly when tapped by accident weeks later. The
issue time rides in the callback data, so a stale press answers
«Устарело» and does nothing.

`pending_memory` is wiped too, though §11's list omits it: it holds text
typed at `/remember` and never classified, and leaving that behind after
"delete all my data" is exactly what build rule 7 forbids.

**One row survives a delete, deliberately** — a single `state_change`
recording that a wipe happened, with no content of any kind. The reset
changes six state fields, and this would otherwise be the only state
mutation in the codebase with no audit behind it. Running `/export`
straight after a `/delete` therefore shows exactly one row.

Two tests in `tests/test_delete.py` are what actually enforce "/delete
must really delete": every table must be either purged or explicitly
kept, and every `user_state` column either preserved or explicitly
reset. Both fail the day someone adds a table or a column and forgets —
which is the only day it matters.

Milestone 3a lays the foundations for Phase 3 — the phase where Anchor
starts speaking first. It ships no proactive message yet: nothing plans
a row, nothing sends one. What it ships is everything that has to be
true *before* that is safe.

- **A clock abstraction** (`app/core/clock.py`). `Clock.now_utc()` is
  now the only source of "now" under `app/core/`, injected from
  `app/main.py` into both the handler path and the job path. The rule
  is enforced by `tests/test_core_clock_discipline.py`, which walks the
  AST of every module in the package and fails on a direct
  `datetime.now()`, `date.today()`, `time.time()` or SQL `func.now()` —
  the same shape as the extractor invariant test below, and for the same
  reason: a rule that lives only in a docstring decays.

  `tests/test_clock.py` pins both Europe/Paris DST transitions the plan
  names. On **2026-10-25** the local day is 25 hours long and
  02:00–03:00 happens twice; on **2027-03-28** it is 23 hours and that
  hour does not exist at all. Those are the two days a naive scheduler
  sends the morning message twice, or never.

- **The `outbound` table**, whose `unique (kind, local_date, bucket)`
  constraint *is* the exactly-once guarantee. Two overlapping processes
  during a Railway rollout, a duplicate heartbeat, or a restart at 09:05
  all collapse into one message because the second insert conflicts.
  Nothing in the send path depends on worker concurrency being 1.

- **The gate** (`app/core/outbound_gate.py`) — a pure function with no
  I/O, and the thing no model can talk its way past. Ten checks in a
  fixed order, first failure wins: kill switch, paused, `/quiet`, quiet
  hours, daily spend cap, ignored-in-a-row, welfare cooldown, daily
  budget, minimum gap while unanswered, then the kind-specific rule.
  `tests/test_outbound_gate.py` is the plan's truth table, one test per
  row, plus precedence (`paused` outranks everything below it — the
  recorded reason has to name the most fundamental cause, because that
  reason is what `/state` will show) and a structural purity check: the
  module imports nothing that could query.

- **The counters.** `last_user_msg_at`, `last_outbound_at`,
  `ignored_in_row` and `welfare_at` on `user_state`. The inbound stamp
  lives in `app/worker.py::process_one_update`, not in a router
  middleware, because that function is the only caller of
  `dp.feed_update` in the repo — so it catches plain text, slash
  commands, button presses, and even an update no handler matches. A
  middleware on the message observer would miss callback queries, and
  tapping through a check-in is the user being present just as much as
  a sentence is.

Milestone 3b is where Anchor starts speaking first. Two fixed intents:
a **morning message** naming the day's main action (or offering to pick
one), and an **evening nag** with a working **Чек-ин** button, sent only
when no check-in happened that day.

- **The heartbeat** (`app/core/scheduler.py`) — a third
  `asyncio.create_task` in the existing worker, on the recovery sweep's
  model. It plans and never does: a few indexed `SELECT`s and at most
  one insert per minute. Everything slow is a row in the Phase 2 `job`
  table, claimed by the loop that already takes inbound updates first,
  so a reply always outranks a proactive message. No APScheduler, no
  second process, no new dependency.

- **Planning is safe to repeat.** The insert is `ON CONFLICT DO
  NOTHING` against `unique (kind, local_date, bucket)`. A 3-hour grace
  window gives the heartbeat ~180 chances to plan the same morning
  message; all 180 collapse into one. `tests/test_scheduler.py` runs
  960 heartbeats across a simulated day and asserts exactly two rows.

- **A refused gate inserts nothing** — not a `skipped` row. That is
  what lets `/quiet 2h` at 08:55 expire at 10:55 and still get the
  morning message, inside grace. A row would have made that impossible.

- **The evening nag's window is clamped to `QUIET_START`**, and so is
  its jitter. Planned at 22:29 with 15 minutes of jitter, the naive
  answer is 22:44 — inside quiet hours, where the send-time gate would
  refuse it, so the message would be silently dropped rather than sent
  slightly late. The clamp picks "late" over "never".

### Generation happens once; sending may repeat

`app/core/outbound_send.py` is built around one asymmetry. Generating
costs money and is not idempotent. Sending is cheap and repeatable. So
a commit separates them:

    insert message (sent_at NULL) + ledger   <- commit
    send to Telegram
    set sent_at, status='sent', counters     <- commit

A crash in the middle leaves a stored message with `sent_at` NULL, and
the re-run **resends the stored text instead of regenerating**. The
user gets the message they were owed and the model is paid once. A
duplicated send is a nuisance; a duplicated generation is money.

**The gate runs twice, and the second run wins.** Minutes pass between
planning and sending because of the jitter, and in those minutes the
user can check in, type a pause word, or set `/quiet`. The re-check
happens *before* the model call, so a nag made redundant at 22:10 costs
nothing at all.

**A failed generation sends nothing.** `status='failed'` and silence —
no canned fallback. A message the user did not ask for has to earn its
place, and boilerplate does not.

**No welfare classifier and no extractor.** Both react to something the
*user* said, and there is no user input here. Running the extractor on
Anchor's own words would let it propose facts about the user from a
message the user never sent.

### The hidden flag is a user turn, not a flag line

Every other hidden flag in this codebase is a `[флаги]` line in the
"## Сейчас" block. The outbound flags are the trailing **user** message
instead, for a practical reason: there is no user message here, and the
chat template this bot runs against expects a conversation that ends
with one. The flag is never stored — only Anchor's reply is — so it
cannot leak into a later transcript.

Outbound messages **do** go into the persona transcript (plan §7) and
into scene summaries. Without that, Anchor would send essentially the
same morning message every day, having no memory of the previous one.
That also required fixing `_load_transcript`: it excluded the current
turn with `update_id IS DISTINCT FROM :id`, which is *true* for every
non-null row — so passing `None` (a proactive message has no update_id)
used to mean "drop every outbound and canned row".

### cancel_outbound, and why the jobs are left alone

`cancel_outbound()` is real now: every `planned` row becomes
`cancelled`. A HARD pause, a welfare trigger and `/delete` all call it;
`/quiet` joins them in 3c.

The pending jobs are deliberately **not** deleted. A job whose row is
no longer `planned` already exits at step 1 of the send path — before
the gate, before the model, before anything is spent. One mechanism,
checked in the one place that matters, rather than two things to keep
in sync.

Status, not deletion, for the same reason: a cancelled row is the
record that a message *was* going to be sent and was revoked. It is
what `/state` will show in 3c, and it is what stops the heartbeat
re-planning the same intent sixty seconds later.

### The counters write no audit row, on purpose

`app/core/state.py::update_state` has been the only sanctioned writer of
a `user_state` field since 1b, and it always pairs the write with a
`state_change` row. 3a adds one deliberate exception, `set_counters`.

These four columns are not decisions, they are traffic bookkeeping:
`last_user_msg_at` and `ignored_in_row` change on *every* inbound
update. Auditing them would turn `state_change` — a short, readable log
of things that were chosen — into a message-rate counter that buries the
rows a human actually wants to read, at two extra inserts per message on
the reply path.

What keeps the exception from widening is `COUNTER_FIELDS`, an
allow-list rather than a denylist: a sensitive column added later is
excluded by default, which is the direction an accident should fail in.
`tests/test_outbound_counters.py` asserts that `persona_active`,
`intensity`, `focus_on`, `due_action` and `streak` are not in it, and
that passing one raises without writing anything on the way.

### The scheduler did not get its own process

Phase 3's heartbeat is a third `asyncio.create_task` in the existing
worker, modelled on the recovery sweep — not a second service and not
APScheduler. It plans; it never does. Its whole job is a few indexed
`SELECT`s plus at most one `INSERT ... ON CONFLICT DO NOTHING` and one
`enqueue_job`. The actual sending is an ordinary row in the Phase 2
`job` table, claimed by the same loop that already calls
`process_one_update()` before `process_one_job()` on every iteration —
so an inbound message outranks an outbound send by a priority rule that
already exists and is already tested, rather than by a new fairness
story invented for the occasion.

### Pause words still come first

Plan §13 puts pause words before everything — `awaiting` states
included. The check-in note step is the only place in this codebase
where plain text means something other than "talk to me", so the note
branch sits at exactly one point in `turn.run()`: after `pause.match()`,
before the raw text is stored. A safeword typed at the note step pauses
the persona and is never filed as a note; a soft `жёлтый` does the same,
because §9's "pause words always win" is unqualified.

Any slash command also clears a pending step. That is enforced by an
outer middleware on the message observer rather than a line in each of
the fourteen command handlers — a blanket rule deserves a blanket
mechanism, and it fires even for a command no handler matches.

A note step left open overnight expires on the local-date boundary, so
tomorrow's first message is an ordinary message rather than yesterday's
note. §9 doesn't specify that; left literal it is a silent data-loss
bug.

### The extractor cannot write sensitive state

Plan §13: `intensity`, `focus_on`, `due_action`, `streak` and
`persona_active` change only via commands, buttons, pause handling or
check-in logic — never via model output. That is enforced structurally,
not by care. `app/core/extract.py` imports exactly three things that can
write: `memory.write_memory`, `proposal.create`, and a `Journal` row. It
does not import `update_state`, and `proposal.accept()` — the only
function that touches a sensitive field — is not in its namespace at all.

`tests/test_extract.py` pins this two ways: a scripted extractor reply
that tries to set `intensity`, `persona_active`, `streak`, `focus_on`
and `due_action` must change nothing, and an AST check asserts the
forbidden names appear nowhere in the module's executable code (its
docstrings discuss them at length, so a plain grep would pass or fail
for the wrong reasons).

Rule memories are never auto-written at any confidence — a rule is the
user instructing themselves, so it always becomes a proposal.

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
