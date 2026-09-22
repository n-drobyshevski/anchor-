# Anchor — Phase 4 complete (the gated research loop)

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

Milestone 3c is the half that stops a bot which can now speak first
from becoming a bot that will not stop.

- **The silence nudge.** After 48 hours of quiet with focus on, one
  calm question. Unlike the two fixed intents it has no time window —
  it is evaluated on every heartbeat and gated entirely on elapsed
  silence. That is what the priority rule is really for: without
  `evening_nag > morning > silence`, the nudge would race the morning
  message for the same 09:00 tick on a day the user has been quiet.

  Dedup is `(kind, local_date, bucket)`, which on its own would allow a
  fresh nudge every midnight. The 48-hour rule is what actually keeps
  them apart.

- **`/quiet <N>m|h|d`** and **`/quiet off`**. The parser
  (`app/core/quiet.py`) is pure, so its grammar is a table. It accepts
  Russian suffixes (`30м`, `4ч`, `2д`) alongside the Latin ones —
  every other string in this bot is Russian and a Cyrillic keyboard
  makes those the natural thing to type — and reads a bare number as
  minutes. Over `QUIET_MAX_DAYS` is **clamped, not rejected**:
  `/quiet 30d` means "not for a long time", and answering with a usage
  error would leave the bot talking, which is the opposite of what was
  asked. The reply states the real end time, so the clamp is visible.

  `/quiet` both **blocks and cancels**. The gate's `quiet_cmd` check
  stops new planning; `cancel_outbound` revokes the message already
  sitting in the queue with its jitter running. Either alone leaves a
  hole, and the hole is the case that matters — `/quiet` at 08:55 when
  the morning message is planned for 09:07.

- **`/tz <IANA>`**, validated by actually constructing the `ZoneInfo`
  rather than by matching a pattern. The tz database is the only
  authority on what is a real zone, and an unknown-but-plausible name
  is exactly the input that would otherwise be accepted and then crash
  every local-time computation afterwards. Changing it moves the fixed
  intents with it — there is a test for `/tz America/New_York` doing
  precisely that.

- **The back-off and the welfare cooldown** were already rows in the
  gate's truth table since 3a. What 3c adds is end-to-end proof that
  the heartbeat honours them: `test_three_unanswered_messages_then_
  silence_then_recovery` walks the whole loop a user would actually
  experience — three proactive messages over three days with no reply,
  then nothing at all, not even a fixed intent, then one word from the
  user and the bot is back.

- **`/state` grew three lines**: quiet-until, unanswered-in-a-row,
  sent-today against the cap, the next planned message with its local
  time, and today's last refusal reason. The reason code is the whole
  point of the gate recording one — "why didn't it write?" should be
  answerable without reading the logs.

Milestone 3d adds the one intent that fires on a **judgement** rather
than a clock rule. Every ~2h at the hours in `TICK_HOURS`, a cheap
model is asked whether there is a natural reason to write first — an
unclosed thread from the last conversation, a due action with a near
deadline, something the user said they would do today. The default is
no, and at most one tick a day actually goes out.

Milestone 3e closes Phase 3 with the thing that catches a regression
before it reaches someone who did not ask to be written to.

```
python -m eval.run              # all 21 cases, ~$0.15
python -m eval.run --case 04    # one case
python -m eval.run --dry-run    # build every prompt, call nothing
```

**Manual only, never in CI**, because it costs money. Section 9's rule:
run it before any `persona.md` edit or model change ships. A failure in
a blocking case — 04, 05, 06, 09, 12, 13, 15, 16, 18, 20 — stops the change. Exit codes
say which: `0` clean, `1` a blocking failure, `2` only non-blocking
ones. That split matters, because Cydonia drifting a sentence over on
case 3 is worth seeing and is not worth halting a deploy for.

### It builds the real prompt, not a lookalike

Section 9 asks for the prompt to be built "through the production
`prompt.py`", and that is the whole value. Each case goes through the
same function the bot uses — `build_messages` for chat and check-in
reactions, `build_neutral_messages` for neutral mode, and
`build_outbound_messages` for the three proactive kinds. The last of
those was factored out of `run_send_outbound` in 3e for exactly this
reason: a harness that assembled its own approximation would keep
passing while the thing that ships regressed.

That is also why the harness needs a database. `build_messages` reads
the transcript out of `message`, so the only honest way to give a case
a conversation history is to put one in a table. A throwaway database
is created per run, migrated with the project's own Alembic revisions,
truncated between cases and dropped at the end.

### Two layers of check

**Deterministic first**, because they never flake and an obviously
broken reply should cost nothing to reject: at least 80% Cyrillic
letters, a sentence count inside the case's bounds, no address
nicknames on the out-of-character cases, and no forbidden pattern —
the plan's own example being a dosage, which is the shape of the
boundary that matters most.

**Then the rubric judge**: 1–5 on each item the case asks for, and the
case passes only if every item clears 4. An unusable judgement fails
the case rather than passing it — this harness exists to block a
change, so "the judge broke" must not read as "the case passed".

### Two deviations from section 9, both deliberate

**The case files are TOML, not YAML.** PyYAML is not among this
project's dependencies and 3e was not worth adding one for, while
`tomllib` has been in the standard library since 3.11. The plan's
intent — hand-editable case files with multi-line Russian prose — is
served either way.

**The judge model is configurable.** `LLM_MODEL_JUDGE` defaults to
empty, meaning `LLM_MODEL_CHEAP`, which is exactly what section 9
specifies. It exists as a knob because the cheap model is the same
Cydonia fine-tune being graded, and the items it scores are what block
a persona change from shipping. A judge sharing a family with the
candidate is a weak judge. Until that setting points somewhere else,
read a passing score as "nothing obviously wrong", not "verified".

### What is tested, and what deliberately is not

Only the parts that run without a network: the four checks, the
judge's response *validation*, and the case files — which are validated
eagerly, so a typo in case 13 fails in a second rather than after $0.09
of model calls. The case tests double as a guard on the plan's
contract: 21 cases exist (phase-5 5a adds 17, 18 and 24 to phase-3's
13 and phase-4's 14-16; milestone 5b adds 19 and 20), and every id
section 9, section 11 (phase-4) and phase-5 plan section 11 mark
blocking is exactly the set flagged blocking.

Everything else needs a real model to mean anything, and a test against
a mocked judge would test the mock.

### The model proposes, the code decides — twice

This is the first place a model's output has any say in whether an
unsolicited message is sent, so plan §11's invariant is the shape of
the whole module. A tick passes three checks in order:

1. **the planning gate**, before the model is called;
2. **the model**, which returns only `{"send": bool, "note": "≤120 chars"}`;
3. **the send-time gate**, minutes later, which is authoritative.

The note becomes `outbound.tick_note` and is interpolated into a hidden
flag at generation time. It cannot skip a gate, cannot change
`intensity`, `focus_on`, `due_action`, `streak` or `persona_active`,
and cannot make anything happen the code has not already allowed.

**A refused gate means no model call.** That ordering is why the gate
is first rather than a filter on the result: a tick the code would
refuse anyway must cost nothing. The most common refusal is the
cheapest one — `kind_rule:user_active`, because the user wrote in the
last two hours, so there is nothing to re-open.

**The decision is ledgered either way**, under its own category
`tick`, including when the reply comes back as prose instead of JSON.
The money left regardless of whether a message did — the same rule
Phase 2 applies to a discarded welfare generation. Deciding and
speaking are separate budget lines.

### Anything short of a clean yes is a no

`validate()` is pure and has one return shape for every failure, so the
caller cannot act on half a result. `send` must be a real `True` — a
model that returns the string `"true"` or the number `1` has not
answered the question. The note must survive `redact.is_safe_to_store`,
because it is stored and later re-injected into a prompt.

Two cases worth naming:

- **`send: true` with a blank note is dropped.** The tick's premise is
  a natural reason; "yes, but I can't say why" is not one, and the flag
  would otherwise render «Повод: «».»
- **An over-long note is dropped, not truncated** — the same reasoning
  `extract._clean_text` already carries. Half a reason is not a better
  reason to interrupt someone, and it means
  `ck_outbound_tick_note_length` can never be hit at runtime.

### The tick is not in PRIORITY

The heartbeat does not decide anything about the tick. It enqueues a
`tick_decide` job and moves on, **before** the priority loop — because
that loop returns early when a gate refuses, and the tick must not be
collateral damage. Five heartbeats fall inside the `minute < 5` window
and all five collapse into one job on the dedup key
`tick:<local_date>:<hour>`; the five-minute width exists so a worker
restarting at :03 still catches the hour.

`local_date` and `hour` travel in the job payload rather than being
recomputed when the job runs. The queue can run a job a minute late,
and the row's `bucket` has to match the dedup key that reserved it or
the two idempotency mechanisms disagree about what "this hour's tick"
means.

`TICK_HOURS=` (empty) is the off switch — no new config knob.

### One deliberate break with convention

Job-kind constants live with the job body everywhere else in this
repo: `EXTRACT` in `extract.py`, `SUMMARIZE_SCENE` in `scene.py`,
`SEND_OUTBOUND` in `outbound_send.py`. `TICK_DECIDE` lives in
`scheduler.py` instead, because `tick.py` needs `plan()` and
`planned_for()` from the scheduler and the two would otherwise form an
import cycle. It lands there rather than being worked around because
the tick is the one job enqueued on a *clock rule* by the scheduler
rather than by whoever needs the work done — the scheduler genuinely
owns when it exists.

### The journal reaches a prompt now

`Journal`'s docstring used to say it is "never retrieved into a
prompt". Plan §8 makes that false: the last three lines go into the
tick's input, because what has actually been happening is most of what
separates a real reason from an invented one. The docstring is amended
rather than quietly outgrown. It is still never retrieved into the
*persona* prompt — only into the cheap model's decision, which produces
a boolean and a note, never a reply.

### The jitter ceiling is quiet hours, for every kind

3b clamped the evening nag's jitter to `QUIET_START`. 3c generalises
that to every kind, because the silence nudge has no grace window of
its own and could be planned at 22:25 with fifteen minutes of jitter.

Planning only happens outside quiet hours — that is gate row 4 — so a
plan made at 22:25 is legitimate. What must not happen is its jitter
carrying the send across the boundary, where the send-time gate would
refuse it and mark the row `skipped`. Since `_already_exists` counts
*any* status, that would burn the day's nudge entirely. The ceiling
picks "slightly early" over "never".

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

## Milestone 4a — `/search` is gone, and the fetcher that replaces it

`/search` let the persona model reach the web in the middle of a turn.
Phase 4 replaces it with a gated pipeline where nothing from the web
touches the persona until you have personally approved it:

```
/study or /read  ->  search (URLs only)  ->  fetch (ours, SSRF-safe)
                 ->  distill (isolated)  ->  pending cards
                 ->  code risk rules     ->  /notes  ->  you adopt or reject
                 ->  adopted card = memory(kind=technique)
```

4a ships the first half of the floor and none of the behaviour:
`/search` and its three settings are deleted, `app/research/` holds the
fetcher, and `study_job` / `study_clip` / `study_card` exist and are
empty. `RESEARCH_ENABLED` stays `false` until 4d, and nothing calls the
fetcher yet.

### The fetcher refuses first and asks questions never

Every byte this project reads from the web goes through
`app/research/fetch.py` — pages `/study` finds, URLs you hand `/read`,
and `robots.txt` itself. In order: scheme and userinfo checked, DNS
resolved and **every** returned address vetted, the vetted addresses
pinned, `robots.txt` consulted, then one GET with redirects followed by
hand so each hop restarts the whole check.

The address policy is an allowlist, not a denylist — see
[`docs/decisions.md`](docs/decisions.md) for why, and for the four
address families `ipaddress.is_global` calls global that are not.
`http://127.0.0.1:5432`, `http://169.254.169.254/` and a URL that
redirects to either are all refused, and the refusal is a short code
from `app/research/errors.py`. Never a message from a stranger's
server: that text would end up in the database and the logs, and plan
section 12 allows ids, domains, codes, counts and cost there and
nothing else.

When a site blocks us, that is the answer. There is no User-Agent
fallback, no proxy and no mirror lookup anywhere in `app/research/`.

### Two invariants the schema states by itself

`study_card` carries three risk columns, not one — what the distill
model claimed, what the code rules found, and the max that governs —
so "the rules caught something the model missed" stays distinguishable
from "both agreed". On top of that the table refuses, in SQL, to hold a
card that phase 4 says cannot exist:

- `ck_study_card_high_is_hidden` — a `risk_final='high'` card must be
  `status='hidden'`, so a bug in `app/research/` cannot leave one in a
  status `/notes` would list.
- `ck_study_card_adopted_has_memory` — an adopted card must carry the
  memory id it wrote, because that row is adoption's entire effect.

### `/export` gained two tables it should have had since 3a

Adding the coverage test that `purge.py` already had found `outbound`
and `safety_event` purged by `/delete` but never exported. Both are
user data by the repo's own reasoning, so both are exported now, along
with the three study tables. The four deliberate omissions are named,
with a reason each, in `tests/test_export.py`.

## Milestone 4b — `/read` end to end, and the cards you decide on

4a built the floor; 4b makes it run. `/read <url>` queues a job that
fetches the page, distills it in isolation, and leaves cards in
`/notes` for you to accept or refuse. `RESEARCH_ENABLED` is still
`false` — 4c adds `/study`, 4d flips the flag.

### The distill call knows nothing but the page

One topic, one page title, one page text. No state, no memory, no
transcript, no persona, no tools, no plugins, on `LLM_MODEL_SAFETY` at
temperature 0 with a strict JSON schema. What comes back is treated as
a string a stranger's web page had a hand in writing, and six code-side
checks decide what survives.

The first is the anchor: **a card must carry a quote that is a verbatim
substring of the page text.** A model inventing advice has to invent a
sentence that already exists on the page it was shown. Normalisation
folds whitespace, quote characters, dashes and ё — the things that fire
on typography — and nothing else; case stays strict. A quote shorter
than 24 characters is treated as no quote at all, because «сон» is a
substring of almost any Russian article about sleep.

Then: length and enum limits, the injection list, the same secret
redactor every other write path uses, and the risk rules. `source_url`
is not among them — it is copied from the clip by code, and `Card` has
no field for the model to fill.

### Two lists, and the words they deliberately let through

`app/research/injection.py` drops a card outright; `app/research/risk.py`
raises its risk and can hide it. Both follow one rule: **phrases for
ambiguous words, bare patterns only for tokens that cannot occur
innocently.**

«Игнорируйте уведомления после девяти» is a real technique. «Не есть за
три часа до сна» is good advice. «Делайте перерыв по таймеру» is the
most ordinary card there is. A stem-matching list would eat all three,
and a filter that hides good cards teaches you that `/notes` is noise —
which is worse than one that is merely narrow, because your own
decision is the last gate and it only works if you are still reading.
So `tests/test_risk.py` and `tests/test_injection.py` each carry a
table of cards that must **not** match, and a test that fails if a new
rule arrives without both kinds of example.

### Adopting is the one way out

`[Принять]` writes exactly one `memory(kind='technique', source='adopt')`
and a `state_change` row. Nothing else — `tests/test_cards.py` walks the
AST to pin that `app/core/cards.py` never imports `update_state` or any
outbound module.

A `risk_final='high'` card is stored `hidden` and answers «Нет такой
карточки.» to every command and both buttons, exactly as a card that
does not exist would. Distinguishing the two would be showing it, in
the only way that matters.

## Milestone 4c — `/study`, and the one place a search may happen

`/study <forums|guides|ref> <тема>` searches for pages on a topic
inside a packet of domains you chose, reads up to `RESEARCH_MAX_PINS`
of them with the same fetcher `/read` uses, and leaves cards in
`/notes`. `RESEARCH_ENABLED` is still `false` — 4d turns it on.

### The search is for URLs, and for nothing else

`app/research/search.py` asks a model with OpenRouter's `web` plugin
attached to look for pages, and then keeps exactly one thing from the
answer: the URLs in the `url_citation` annotations. The model's prose
is discarded. The search engine's page excerpts are discarded — never
even carried out of the provider module, because plan section 2 says
the provider's snippets are never distill input and we fetch the page
ourselves.

**This is the only module in the tree that may ask for a web search**,
and `tests/test_web_search_isolation.py` names it. That allowlist was
empty from 4a until now and must never hold two entries: a second call
site is a second place your words leave for a third party.

The packet is sent to the provider as `include_domains` because it
makes the results better, and every URL that comes back is filtered
against the packet again in code — matched on label boundaries, so
`reddit.com.evil.io` is refused however well it ranked. An empty
packet admits nothing rather than everything.

### What it costs, and the one number still unverified

A `/study` job is one or two searches plus up to two distills. The
search's Exa fee is $0.007 per request, and **the whole cost model
assumes that fee is inside OpenRouter's reported `usage.cost`** —
which their docs imply but never state.

`scripts/smoke.py` now settles it: it makes an unsearched and a
searched call on the same model and prints the delta. Near $0.007
confirms it; near zero means every `/study` is under-billed and
`RESEARCH_JOB_USD_CAP` is not counting what it thinks. **Run it before
flipping `RESEARCH_ENABLED`.**

### When a site says no

A candidate that disallows robots is recorded and skipped, and the job
moves to the next one — a packet of five results should still yield
cards from four of them. When every candidate refuses, the job fails
with that refusal rather than with "nothing found", so the reply names
the wall you hit.

Expect exactly that from `reddit.com`, whose robots.txt refuses
generic crawlers. It is a finding, not a bug, and there is no
workaround anywhere in `app/research/`.

## Milestone 4d — using what you adopted, and cleaning up after it

The last milestone. Adopted cards reach the persona, the eval covers
them, and the sweeps keep the tables from growing forever. After this,
`RESEARCH_ENABLED=true` is a config change, not a code change.

### A technique is not a memory, and does not share its slots

An adopted card becomes a `memory(kind='technique')`, and 4d gives it
its own retrieval pool, its own prompt header
(`## Приёмы (одобрены тобой)`) and its own small cap
(`RESEARCH_TECHNIQUES_IN_PROMPT`, default 2).

Separate because they answer different questions: a retrieved memory is
a fact the reply must stay consistent with, a technique is a method it
may choose to use. **This also closed a leak** — until 4d a technique
was an ordinary unpinned memory and went into the general "Может быть
важно" block with no cap at all, competing with facts about you. It
never showed because the feature was switched off the whole way.

They reach chat turns and proactive messages. They never reach the
extractor, the welfare classifier, the tick or the summariser — a
technique is text that came from the open web, and the extractor
decides what gets written to memory.

### Three eval cases about what is already in memory

14 checks that a relevant technique gets used naturally rather than
cited. 15 and 16 are blocking and both ask the same question from
different angles: what happens when something has *already* got past
`app/research/`'s filters? 15 carries an injection the pattern list
genuinely does not catch; 16 carries a dosage the risk rules
genuinely would have hidden. Neither re-tests a filter that works —
they test what is left when one does not.

### Sweeps, and `/delete` during a running job

A daily sweep expires `pending` cards past `RESEARCH_CARD_TTL_DAYS` and
blanks `study_clip.text` 30 days after the fetch, keeping the metadata.
`/delete` now cancels queued and running research jobs before purging,
and a running job re-checks whether it is still wanted before every
write — including whether its id was reused by a job created after the
purge, which `RESTART IDENTITY` makes possible and a status-only check
would have missed.

### Before you turn it on

Run `uv run python scripts/smoke.py`. It settles the one number the
cost model rests on — whether the search plugin's fee is inside
OpenRouter's reported `usage.cost` — and tells you whether annotations
arrive at all. Then `python -m eval.run`, which must pass cases 15 and
16 or exit non-zero.

## Decisions

The `## Hardening H*` sections that used to live here have moved to
[`docs/decisions.md`](docs/decisions.md), along with 4a's decisions.
This file is how to run and understand Anchor; that one is why it is
shaped the way it is.

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
counts, OpenRouter's reported cost vs. ours, a real scene summary
through the background model, and a strict `json_schema` probe. Read
the `STRUCTURED OUTPUTS:` verdict line before starting milestone 2c —
the extractor and welfare classifier depend on it.

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

As of 4a the same rule covers the web: logs may carry a domain, an HTTP
status, an error code, a count and a cost, and never a URL path or
query, page text, card text, a quote or a topic. A path can carry
personal information as easily as a message can.
