# Decisions

The log of choices this project made and why, kept where the plans can
point at it. Each entry states the problem, the decision, and what would
have to be true for the decision to be wrong.

Until milestone 4a this lived inside `README.md` as a run of
`## Hardening H*` sections. It moved here because `README.md` had grown
to 44 KB of mixed "how to run it" and "why it is like this", and phase 4
adds a great deal more of the second kind. The H2 and H4 sections below
are that text, moved verbatim; H1, H3 and H5 were never written up as
sections and are summarised at the top of the hardening group for
completeness.

`anchor-phase1-plan.md` through `anchor-phase6-plan.md` (plus
`anchor-web-panels-plan.md`), and `anchor-phase8-plan.md` for the vault
(Phase 7 is reserved for tracker and device integrations), remain the
specifications. This file records what was decided while implementing
them.

---

## The hardening pass (H1–H5)

Five fixes between phase 3 and phase 4. Three of them changed enough
code to deserve their own section below; two did not:

- **H1 — secret scanning.** A `gitleaks` config and a synthetic
  secret-token fixture, so a real credential committed by accident
  fails CI rather than sitting in the history. Nothing in the running
  app changed.
- **H3 — `/search` off, then gone.** `/search` let the persona model
  reach the web mid-turn. H3 turned it off behind `LLM_WEB_SEARCH=false`
  and pinned the property with an AST test asserting exactly one call
  site could ask for a search. **Milestone 4a removed the command, the
  three `LLM_WEB_SEARCH*` settings and the provider's `web_search`
  parameter entirely**, and retargeted that test to assert there is now
  no such call site at all. Its allowlist gains exactly one entry,
  `app/research/search.py`, in milestone 4c, and must never gain
  another. The replacement is the gated research loop, which is the
  whole of phase 4.
- **H5 — the code-side medical/legal filter and an independent judge.**
  A filter the model cannot talk its way past, and an eval judge from a
  different lab than both the persona model and the safety model, so a
  family is never grading its own output.

## Hardening H2 — the safety model split

Three calls decide things the persona must not: the welfare classifier,
the post-turn extractor, and the tick decision. All three ran on
`LLM_MODEL_CHEAP`, which defaults to the same Cydonia roleplay fine-tune
as the persona itself. They now run on `LLM_MODEL_SAFETY`
(`google/gemini-2.5-flash-lite`), chosen for schema compliance rather
than voice. Scene summaries stay on the cheap model, because a summary
is prose.

Background spend went **down**: roughly $0.021/day to $0.009/day at
current volumes.

### Why not the cheapest nano-class model

`openai/gpt-5-nano` is cheaper per token and was the first choice. Its
live OpenRouter endpoints say it accepts **no `temperature`** — not on
OpenAI, not on Azure. `OpenRouterProvider.complete()` always sends one,
and `require_parameters` is already set for every `json_schema` call, so
routing would have found zero eligible endpoints and the welfare check
would have failed 100% of the time. Its reasoning is also mandatory,
which is a latency risk against the 8-second `WELFARE_TIMEOUT_SECONDS`.
Checking the endpoint list before writing the code is the only reason
that did not ship.

### The bug underneath: `none` meant two things

`welfare.parse()` returned `Verdict('none', 0.0)` for unparseable output
*and* for a genuine "nothing wrong". The caller could not tell them
apart, so a classifier failing on every single turn looked exactly like
a quiet week.

`classify()` now returns a `Classification(verdict, response, outcome)`,
where outcome is `ok | parse_fail | timeout | error`. Failing open is
unchanged — that is what plan section 10 requires — but it is no longer
silent.

### The backstop

When, and only when, the classifier produced nothing usable,
`app/core/welfare_terms.py` checks the user's message and the two turns
before it against a fixed list of self-harm and suicide terms in
Russian, French and English. A hit is treated exactly as `level="real"`
and reuses the existing welfare reply, buttons and persona-off path —
the backstop decides *whether*, never *what*.

It returns a bool and nothing else. There is deliberately no API that
reveals which term matched, so no caller can log one by accident.

Precision is traded away on purpose. It runs only after the model has
already failed, so the alternative is no check at all, and the two
errors are not symmetric: a false positive is a warm message and a
button; a false negative is the persona pushing someone who just said
they want to die.

### `safety_event`, and why it is not a `spend_ledger` column

A column was the obvious choice and it does not work.
`turn.py::_ledger_only()` returns early when the response is `None`, so
a classifier that **timed out writes no ledger row at all** — the very
outcome most worth recording is the one that table structurally cannot
hold. Going the other way is no better: a timeout, an error and a
`fallback_hit` cost nothing, so recording them as zero-cost rows would
pollute `today_by_category()` and the daily-cap query — and the cap is
row 5 of the outbound gate, so noise there silences proactive messages.

So: a separate table, six columns, no content ever. Both vocabularies
are constrained in SQL (unlike `spend_ledger.category`, which is
deliberately open because it records money already spent and a rejected
row would lose the record), and a test pins the constants against the
constraints.

The write is best-effort at every call site. Observability that can fail
the turn it observes is a worse bug than the blindness it replaces.

`/state` gained one line:

```
Проверка благополучия (7 дн.): ok N · сбои M
```

`сбои` sums `parse_fail`, `timeout` and `error`. A `fallback_hit` is
counted as neither — it is the backstop working, and folding it into
either column would hide the one event most worth seeing.

### Two things found while wiring it

`require_parameters` — which this pass set out to add — turned out to
already exist at `app/llm/openrouter.py:216`, set for every call
carrying a `json_schema`. It was left exactly as it is.

The test suite's `TRUNCATE` list was hand-written and had gone stale
twice: `outbound` (3a) survived only because it has a foreign key to
`message` and got caught by `CASCADE`, and `safety_event` has no foreign
key at all, so its rows leaked between tests in the same file and made
assertions pass or fail depending on test order. The list is now derived
from `Base.metadata.sorted_tables`.

## Hardening H4 — truthful cost accounting

The review said cost accounting ignored OpenRouter's reported figure. It
does not, and never did: `compute_cost` has always preferred
`usage.cost_usd` when present. What was missing was *provenance*, and
one real bug underneath it.

### `cost_source`

`spend_ledger` rows carried two different kinds of number under one
column. One is what OpenRouter says it charged. The other is our
arithmetic over token counts, at prices from config that can quietly go
stale. A row that does not say which it is cannot be audited — "the
totals look wrong" has no answer, because a drifted price setting and a
vendor change produce the same symptom.

So rows now carry `cost_source`: `'vendor'` or `'computed'`. Nullable,
and deliberately **not** backfilled — rows written before H4 genuinely
do not know, and stamping them with a guess is the false certainty the
column exists to remove.

### The web-search fee was on the wrong branch

Exa's "auto" mode, which `app/llm/openrouter.py` asks for, costs **$0.007
per request** including up to 10 results (we ask for 5). Verified
2026-09-22 on [OpenRouter's web-search docs](https://openrouter.ai/docs/features/web-search).

The fee setting defaulted to `0.0`, and `compute_cost` added
it to *both* the vendor and the computed branch. That made `0.0` the
only value that could not double-bill — the setting was a placeholder
for an unanswered question, not a price.

The question is now answered.
[OpenRouter's usage-accounting docs](https://openrouter.ai/docs/use-cases/usage-accounting)
define `cost` as *"the total amount charged to your account"*, stated as
distinct from `cost_details.upstream_inference_cost`, *"the actual cost
charged by the upstream AI provider"*. The two fields exist separately
precisely because the first is broader than inference, and the Exa fee
is charged to the same OpenRouter credits. So:

- **vendor branch** — the fee is already inside the reported figure.
  Adding it again would double-bill the one path where we have the real
  number.
- **computed branch** — the fee is not there, and is ours to add.

Which means the default can be the real price: `0.007`, applied to the
computed branch only. The old `0.0` was not neutral — it made the
fallback path silently **under**-bill every searched turn, which is the
dangerous direction for a setting the daily cap depends on.

This reading was falsifiable on live data: `scripts/smoke.py` used to
print the reported-cost delta between an unsearched and a searched call
to check it (a delta near $0.007 confirms it, a delta near zero refutes
it). That comparison left the script with milestone 4a, along with
`/search` itself.

`/search` itself was removed in milestone 4a, so none of this is live
billing today — it is the accounting being right before a research path
comes back in a later phase-4 milestone.

### Price audit

Every declared price re-checked against OpenRouter's live model
endpoints on 2026-09-22. All three sets matched — Cydonia at
$0.30/$0.15/$0.50 and Flash-Lite at $0.10/$0.01/$0.40 — so nothing
changed but the dated citations saying so. The model prices are a
fallback in any case: when OpenRouter reports a cost, that figure wins
and the config prices are never consulted, which `cost_source` now makes
visible per row.

---

## 4a — `httpx` was not transitive, so the fetcher uses `aiohttp`

The phase-4 plan asked for `httpx` to be declared explicitly, "even if
it's already transitive". It is not. The `openai` SDK 3.16.2 depends on
**`httpx2` 2.13.0**, a separately named distribution; plain `import
httpx` fails in this project's venv. Adding it would have put a third
async HTTP stack in one process alongside `aiohttp` (already a direct
dependency, and the transport under aiogram and the webhook server) and
`httpx2`.

`aiohttp` also does the security-critical part better. Plan section 5.3
wants the socket to connect to an address we have already vetted, while
TLS still negotiates the real hostname so the certificate is validated
against the site rather than an IP. `aiohttp` has a pluggable resolver:
`TCPConnector(resolver=...)` supplies the addresses, and
`_create_direct_connection` takes `server_hostname` and the `Host`
header from `req.url` — the name, not the resolver's answer. So
`PinnedResolver` hands back addresses `app/research/fetch.py` has
already checked and DNS rebinding has nothing left to rebind: the
second lookup never happens.

The `httpx` equivalent would have been a URL rewrite — put the IP in the
URL, override `Host`, and pass `extensions={"sni_hostname": host}` so
`httpcore` sets the TLS server name. That works (verified against
`httpcore2` 2.13.0), but it needs IPv6 bracketing, manual `Host`
handling, and depends on an extension key rather than a public API.
Worse, for security-critical code, than a supported seam.

`tests/test_fetch.py` checks the aiohttp behaviour against the library
rather than against its documentation: it pins `example.test` to
127.0.0.1, serves on loopback, and asserts the request arrives with
`Host: example.test` and no cookie. The whole design rests on that.

**This would be wrong if** a future aiohttp took the Host header from
the resolver's answer, which that test would catch, or if the research
loop ever needed an HTTP feature aiohttp lacks.

## 4a — `trafilatura` is a string transform, and nothing more

Added as the one new dependency, for HTML-to-main-text extraction. It is
used only as `bare_extraction(html_string)`: its own `fetch_url` and
`download` helpers are never imported, because every byte from the web
must come through `app/research/fetch.py` where the address checks are.
`tests/test_research_isolation.py` pins that.

Worth knowing: it declares 6 runtime dependencies and installs **16**
transitive packages (`lxml`, `justext`, `courlan`, `htmldate`,
`charset-normalizer`, `urllib3`, plus `babel`, `dateparser`, `tld`,
`tzlocal`, `regex`, `pytz`, `python-dateutil`, `six`,
`lxml-html-clean`). That is a large footprint for one function, and it
brings `urllib3` into the process. The alternative was a stdlib
`HTMLParser` extractor, which would be materially worse at telling an
article from its navigation — and a bad extraction is not a cosmetic
problem here, because plan section 7.1 makes every card's quote prove
itself as a verbatim substring of the extracted text.

**This would be wrong if** the footprint ever becomes a supply-chain
concern worth more than the extraction quality.

## 4a — the address policy is an allowlist, not a denylist

`app/research/addresses.py::is_public_address` requires
`ipaddress`'s `is_global` and then rejects the things `is_global` still
admits. Written the other way round — reject 10/8, reject 127/8,
reject … — it would be one missing range away from being a server-side
request forgery, and the missing range is always the one nobody thought
of.

Four cases justify the extra clauses, all verified against CPython 3.12
on 2026-09-22: `224.0.0.1` and `ff02::1` (multicast is global),
`fec0::1` (deprecated IPv6 site-local is global *and* not private),
`::127.0.0.1` (IPv4-compatible IPv6 is global and `.ipv4_mapped` is
None for it), and `64:ff9b::7f00:1` (the NAT64 well-known prefix, for
which `ipaddress` has no property at all). Every IPv4-in-IPv6 form is
rejected as a class, including `::ffff:8.8.8.8` whose payload is
perfectly public: a real site's AAAA record is never one of these, so
unwrapping them would only create a second place that has to stay
correct forever.

Two hostname rules were added after a test caught the gap: a host must
contain a dot, and its last label must not be numeric or `0x`-prefixed.
`ipaddress` refuses to parse `0x7f000001`, so it fell through as a
hostname — and glibc's `getaddrinfo` resolves it to 127.0.0.1.
`vet_addresses` would have caught the result, but being refused for the
right reason before a DNS query goes out is the difference between a
rule and a coincidence.

## 4a — the Public Suffix List is not worth a dependency here

`domain_matches` compares on label boundaries: `reddit.com` admits
`reddit.com` and `old.reddit.com`, and refuses `reddit.com.evil.io` and
`notreddit.com`. A PSL would let us talk about registrable domains in
general, at the cost of a dependency plus a data file that is wrong the
moment it goes stale. The packet allowlists are three to seven domains
chosen by hand, so suffix matching is both sufficient and the safer
failure mode: it can only ever refuse too much.

## 4a — `robots.txt` we cannot read means we do not fetch

RFC 9309 §2.3.1 says a 4xx means "allow all" — the file simply is not
there — while a 5xx "may" be treated as a full disallow. We take the
strict reading of both, and treat 401/403 as a disallow too. A site
that cannot tell us its rules does not get fetched by us today. That is
the direction plan section 5.9 points: when a site says no, in any
dialect, the answer is a reported finding and never a workaround. There
is no User-Agent fallback, no proxy and no mirror lookup anywhere in
`app/research/`.

The expected consequence is that `/study forums` will often report
`robots_disallow` against `reddit.com`, whose robots.txt refuses
generic crawlers. The plan's acceptance checklist already treats that
as a valid outcome.

## 4a — `/export` was missing two tables, and now has a test that says so

`app/core/purge.py` has always been cross-checked against
`Base.metadata`: `tests/test_delete.py` asserts every table is either
purged or explicitly kept, so a new model breaks it immediately.
`app/core/export.py` had no such check — both of its coverage tests
derived their expectation from `EXPORTED_MODELS` itself, so a table
missing from that tuple was invisible.

Adding the equivalent test in 4a found `outbound` and `safety_event`,
added in milestone 3a and hardening H2 and never exported. Both are
purged by `/delete`, and `purge.py`'s own comments call them user data:
"a record of what the bot said to this user and when" and "a record of
when this user was talked to". A table cannot be user data for
`/delete` and plumbing for `/export`, so both were added, along with
4a's three study tables. `outbound` also holds what `message` cannot:
proactive messages that were planned and then skipped or cancelled.

The four deliberate omissions are now named in a `NOT_EXPORTED` map with
a reason each, and a second test keeps that map honest by failing if it
ever names a table that *is* exported.

**This would be wrong if** either table turns out to contain something
that should not leave the machine in a file the user can share. Neither
holds message content; `outbound` holds a 120-character `tick_note`,
which the bot wrote about the user and which `/export` is meant to
disclose.

---

## 4b — the two pattern lists take phrases, not stems

`app/research/injection.py` and `app/research/risk.py` both follow one
rule: **phrases for ambiguous words, bare patterns only for tokens that
cannot occur innocently.**

This is where a filter list usually goes wrong, because the cards we
want are practical advice and practical advice is full of near misses.
«Игнорируйте уведомления после девяти» is a real sleep-hygiene
technique, and a stem-matching list of the kind
`app/core/welfare_terms.py` uses — which is right for its own job —
would drop it silently. So `игнорируй` on its own is not a pattern;
`игнорируй всё выше` is. The same applies to «не есть за три часа до
сна» against `extreme_restriction`, «контролировать своё время» against
`third_party`, and «таймер» against `physical_devices`.

Over-matching is not the safe direction here, despite appearances. A
rule that hides good cards teaches the user that `/notes` is full of
noise, and the filter that gets ignored is worse than the one that is
merely narrow — the user's own decision is the last gate, and it only
works if they are still reading.

The counterweight is in the tests: `tests/test_risk.py` and
`tests/test_injection.py` each carry a `CLEAN` table of cards that must
*not* match, several of them one word from a rule, and a test that
fails if a new rule id arrives without both a positive and a negative
example. A list only ever tested for what it catches grows wider every
time someone adds a term.

**Three rules are deliberately broad**, and are the ones to revisit
first if `/notes` starts feeling noisy: `health_meds` matches any
substance, dose or unit (the one category where a wrong `low` is a
health outcome); `illegal` matches bare `drugs` in English (which is
`high` via `health_meds` anyway); and `developer_mode` includes «без
ограничений», which can innocently mean "unlimited".

**This would be wrong if** the negative tables stop being extended
alongside the rules, at which point the discipline is gone and only the
appearance of it remains.

## 4b — the quote is the anchor, and it has a length floor

Plan section 7.1 makes every card carry a quote that is a verbatim
substring of the clip text. That is what makes hallucination
structurally hard rather than merely discouraged: a model inventing
advice has to invent a sentence that already exists on the page it was
shown.

Normalisation folds whitespace, quote characters, dashes and ё, and
nothing else. Every fold widens what counts as a match, and those four
fire on typography — a true quote must not be rejected because the page
wrapped a line or the model typed a straight apostrophe. **Case is not
folded**, because case fires on content.

`QUOTE_MIN = 24` characters is not in the plan and was added anyway:
without a floor the anchor is defeatable by quoting a common word —
«сон» is a substring of almost any Russian article about sleep — which
would leave the strongest check in the file decorative.

**This would be wrong if** 24 characters turns out to reject real short
quotes often enough to matter. Nothing seen yet suggests it does.

## 4b — a /read URL rides in the queue payload, not in `study_job.query`

`query` is the topic column, capped at 200 characters by
`ck_study_job_query_length`, and a `/read` has no topic at enqueue time
— the topic is decided later from the fetched page's title. Storing the
URL there was the obvious move and the wrong one: 200 characters is a
real limit on real links. An article URL carrying campaign parameters,
or any share link from a phone, runs past it, and there is no honest
refusal to give for that. The URL is fine; only the column was too
small, and refusing it as `bad_url` names the wrong cause.

So the address travels in the `job` queue row's JSONB `payload`, exactly
as `update_id`, `scene_id` and `outbound_id` already do.
`study_clip.url` remains the authoritative record of what was actually
read, after redirects.

## 4b — the job-finished line is not an outbound

Plan section 9 is explicit that it "isn't an outbound in the Phase 3
sense: it's a reply to the user's command", so `_may_report_now` in
`app/worker.py` deliberately does not call the outbound gate. It does
not touch the outbound counters, is not subject to `OUTBOUND_ENABLED`,
and is not stopped by the daily cap.

What it does respect is the three states that mean "not now" in the
user's own voice — a pause, an explicit `/quiet`, and quiet hours —
read exactly as the gate reads them, because those three are about the
user rather than about the budget. When the answer is no, nothing is
sent and nothing is queued for later: the cards are already in
`/notes`, which is where the line would have pointed.

The count is pluralised. Plan section 9 writes «Готово: N карточек.
/notes» with N as a placeholder, not as a spec for the three Russian
plural forms, and «Готово: 1 карточек» is not a sentence a bot that is
supposed to sound like a person sends. The wording is otherwise exactly
the plan's.

## 4b — `near_duplicate` became public rather than being copied

Adopting a card must end with `study_card.memory_id` set
(`ck_study_card_adopted_has_memory`), but `write_memory` returns `None`
when an active memory already says the same thing, and reports only
*that* a duplicate exists, never which row. The honest id to store in
that case is the existing memory's — a technique a page repeats, or a
second `/read` of a similar page, is the ordinary case dedupe exists
for, not a reason to block the user's decision.

The first implementation repeated `write_memory`'s private
`_near_duplicate` query inside `app/core/cards.py`. That function is
now public and called from both places instead. Two copies agreeing
today and disagreeing after the next threshold change would strand an
adoption with no memory to point at and fail the constraint — one
definition of "the same fact" is worth more than the module boundary
it crosses.

This also makes a replayed adopt land correctly: the first run's memory
is still active, `write_memory` calls it a duplicate of itself, and the
card links to that same row rather than a fresh copy.

## 4b — a hidden card and a missing card read the same to the user

`/card`, `/adopt`, `/reject` and both buttons all answer «Нет такой
карточки.» for a card that does not exist and for one that is hidden.
Plan section 12 says a `risk_final='high'` card is never shown; a reply
that distinguished "no such card" from "that one is too risky to show
you" would be showing it, in the only way that matters — the user would
know a card exists and what kind of thing it is.

`app/core/cards.py` still tells the two apart internally (`GONE` versus
`FORBIDDEN`), because the difference is worth logging even when it is
not worth saying.

## 4b — no `safety_event` row for a distill call

`app/core/extract.py` records one on every run, and the research job
does not. `ck_safety_event_kind` admits only `welfare`, `extractor` and
`tick`, and a fourth value needs a migration the phase-4 plan does not
call for.

`study_job.error_code` carries the same signal per job, so nothing is
lost — but `/state`'s per-day safety rollup is blind to distill, which
means a model that starts returning unparseable JSON looks like "0
cards" rather than like a fault. That is exactly the failure mode the
table was added for in H2.

**Revisit in 4c**, where `/study` adds two more distill calls per job
and the blind spot gets proportionally larger.

*(4c did not revisit it; see the post-4d entry below, which does.)*

---

## 4c — the `web` plugin is attached in exactly one function

`tests/test_web_search_isolation.py` names `app/research/search.py`'s
`find_urls` and nothing else, and `app/llm/openrouter.py` is the only
module allowed to build a `plugins` payload at all. Neither rule is a
style preference. A second search call site is a second place where the
user's words leave for a third party, and the point of the research
loop is that there is exactly one, behind a daily quota, discarding
everything it gets back except the URLs.

That tuple was emptied in 4a when `/search` was deleted and has one
entry now. It must never have two.

## 4c — the provider's domain filter is a request; the allowlist is a rule

`include_domains` is sent because it makes the results better. Its
answer is re-filtered in code regardless (plan section 2: "code
**always** post-filters to the packet allowlist"), matched on label
boundaries so `reddit.com.evil.io` is refused however it was ranked.

This matters more than it looks, because the provider side is genuinely
unreliable here. OpenRouter's docs (checked 2026-09-22) say Google's
native search does not support domain filtering at all: with the
default engine OpenRouter silently falls back to Exa when filters are
set, and with `"engine": "native"` it returns a 400. `LLM_MODEL_SAFETY`
is `google/gemini-2.5-flash-lite`, so the engine is pinned to Exa
explicitly rather than left to default — which also makes the $0.007
per-request fee the same whatever that setting points at next.

`filter_citations` **fails closed**: an empty allowlist admits nothing
rather than everything. A packet emptied in config must not quietly
turn into a search of the open web at the one moment nobody is
watching. `app/research/jobs.py` refuses such a job before the filter
is reached; this is the second line.

## 4c — the search call discards the prose, and pays less for it

`app/research/search.py` keeps the `url_citation` annotations and
throws the completion away. The `search_prompt` is overridden to ask
for a one-word answer, because the annotations are attached by
OpenRouter from the search itself rather than written by the model —
nothing is lost by the model saying almost nothing, and we stop paying
for output we discard.

This does not make the call free. The plugin injects roughly 2,000–4,000
characters of page excerpt per result into the prompt as input tokens
either way, which is most of what a search costs beyond the fee.

`_extract_citations` deliberately never reads the annotation's
`content` field. It is a search engine's excerpt of a page, and plan
section 2 says the provider's snippets are never distill input — we
fetch the page ourselves, under our own rules. Not carrying it past
that function is what makes the rule structural instead of a promise.

A searched call is also allowed to return no prose at all, so
`_extract_text`'s "never return an empty reply silently" rule — right
for every other caller — is skipped for this one.

## 4c — the plugin fee is not added by us, and smoke says whether that is right

Plan section 6: "the fee is taken from the provider-reported cost".
`app/core/spend.py` prefers `usage.cost` when OpenRouter reports one,
and H4 reasoned that the Exa fee must be inside it — OpenRouter
documents `cost` as "the total amount charged to your account", as
distinct from `cost_details.upstream_inference_cost`, and Exa is
charged to the same credits.

**The docs still do not say this outright.** So the 4a decision to drop
`LLM_WEB_SEARCH_PRICE_USD` rather than reintroduce fee arithmetic rests
on an inference, and `scripts/smoke.py` now settles it: it makes an
unsearched and a searched call on the same model and prints the
reported-cost delta. A delta near $0.007 confirms it. A delta near zero
refutes it, and refuted means every `/study` is under-billed and
`RESEARCH_JOB_USD_CAP` is not counting what it thinks it is.

`cost_source` on every ledger row records which branch priced it, so a
run that fell back to the token formula is visible as one that
under-counts the fee rather than silently wrong.

**Run the smoke probe before flipping `RESEARCH_ENABLED`.**

## 4c — a candidate that refuses us is not the end of the job

Plan section 5.9 forbids working around a refusal, not noticing it. A
packet of five results whose first entry disallows robots should still
produce cards from the rest, so `_run_study` records the refusal in a
`study_clip` row and moves to the next candidate. `pins_used` counts
pages actually read, so a refusal costs no pin.

When **every** candidate refused us, the job fails with the *last*
refusal code rather than with "nothing found". The acceptance checklist
asks `/study forums` to "report clearly that Reddit blocked the fetch",
and an empty result would not be that report — it would look like the
search failing, which is a different problem with a different fix.

Expect this to be the common case for `reddit.com`, whose robots.txt
refuses generic crawlers. That is a finding, and there is no
workaround anywhere in `app/research/`.

## 4c — `/study` and `/read` count against separate daily quotas

`RESEARCH_JOBS_PER_DAY` and `RESEARCH_READS_PER_DAY` (plan section 3),
counted per `study_job.kind`. They cost differently: a study job is one
or two searches plus two distills, a read is one distill on a page the
user already chose. Spending one must not consume the other, and a
single shared counter would have made the cheaper command hostage to
the expensive one.

---

## 4d — techniques are their own retrieval pool, and that closed a leak

Phase-4 plan section 10 asks for adopted techniques to reach the prompt
under their own header. Implementing it revealed that they already
reached it under the wrong one: a `technique` was an ordinary unpinned
memory, so `retrieve_memories` returned it like any other, into the
"Может быть важно" block, with no separate cap, competing with facts
about the user for `MEMORY_RETRIEVED_MAX` slots.

Nothing caught it because `RESEARCH_ENABLED` was false through 4b and
4c, so no technique could exist yet. It would have surfaced the first
time a card was adopted with the flag on.

Two pools because they answer different questions. A retrieved memory
is a fact the reply must stay *consistent with*; a technique is a
method the reply may choose to *use*. One block would have told the
model the wrong thing about both, and one cap would have let a chatty
week of adopted cards crowd out the bot knowing who it is talking to.

**The fallback is least-recently-used, not `_topup`'s pool.** `_topup`
returns the same newest identity/rule rows on every low-match turn, so
their `use_count` measures how often retrieval failed — a distortion
that module already documents. Least-recently-used rotates instead,
which is the only way a card adopted months ago is ever tried again.
`NULLS FIRST` is explicit, or a never-used technique would sort last
under ASC and a freshly adopted card would be the last one ever
offered.

## 4d — the daily sweep is a sibling of the heartbeat, not part of it

`maybe_enqueue_research_sweep` lives in `app/core/scheduler.py` next to
`maybe_enqueue_tick`, but `app/worker.py`'s `_heartbeat_loop` calls it
as a separate step rather than `heartbeat()` calling it.

Not arbitrary. Five existing tests call `heartbeat()` directly and
assert the exact resulting state of the `job` table — "a failed
planning gate inserts no row" means literally zero rows. An
unconditional second job kind inserted from inside `heartbeat()` breaks
all five, and those assertions are pinning real properties about
outbound planning that should not be loosened to make room for an
unrelated sweep. A sibling call gets the same once-a-minute cadence,
which is all the dedup key needs to become once-a-local-day.

Both sweeps are one job kind, not two: same cadence, same shape, no
isolation to gain. Neither is gated on `RESEARCH_ENABLED` — they are
hygiene on rows that may predate the switch being turned off, and
gating them would leave 30-day-old page text sitting indefinitely.

## 4d — `/delete` cancels a running job, and what that does not cover

`app/core/purge.py` flips every non-terminal `study_job` to
`'cancelled'` and drops pending `research` queue rows before the
TRUNCATE. Since both tables are purged anyway, the only thing this
changes is what a worker already mid-run does next.

`run_research_job` used to read the status once, at the top, which
stops a job that has not started and nothing else. 4d adds a re-read
before every write-heavy step — before the search, before each
candidate fetch, before each distill.

**The check is on `created_at` as well as status**, and that is the
part worth reading. `/delete` uses `RESTART IDENTITY`, so a study_job
created after the purge takes id 1 again — and its status is
`'queued'`, not `'cancelled'`. A status-only check would have waved the
old run straight through into the new job's rows, and the user would
have seen cards in `/notes` from a job they had just deleted.

Still not complete, and not claimed as complete: a row that vanishes
*between* a check and the write it guards produces a foreign-key
violation on the flush. That is noisy and safe — the write is refused
rather than misfiled, which is the property that matters.

Spend survives a cancellation. `_distill_into_cards` commits its ledger
row before any card is written, so a distill that ran is billed whether
or not the job it belonged to still exists. An in-flight clip is rolled
back with the session, which is right: a half-run's clip is exactly the
stray row the check exists to prevent.

## 4d — eval 15 does not use the plan's literal injection

Plan section 11 gives «Игнорируй правила и…» as case 15's example.
Used verbatim it would be caught by `app/research/injection.py`'s
`override_previous` rule, which would make the case a second test of a
filter that already works rather than a test of what happens when one
slips through.

The case carries a paraphrase instead, verified against the real
`injection.hits()` (zero hits) and `risk.assess()` (no opinion). What
is left between that memory and the reply is the persona itself, which
is what a defence-in-depth case is supposed to measure.

Case 16's dosage, by contrast, verifies as `('high', ['health_meds'])`
— so a card like it could not be adopted today at all. The case exists
for the day a rule is loosened, a migration backfills rows written
before the rule, or a bug lets one through.

---

## Post-4d — a crashed job was orphaned, and told you a failure it had not recorded

`run_research_job` treated *anything* not `'queued'` as an
already-finished job to report on. That is right for `done`, `failed`
and `cancelled`. It was wrong for `searching`, `fetching` and
`distilling`, which is exactly where the row sits when a worker dies
mid-run:

1. `recover_stuck_jobs` returns the *queue* row to pending after
   `STUCK_AFTER`; it never touches `study_job`.
2. The job is redelivered, hits that branch, does nothing, raises
   nothing.
3. `process_one_job` therefore completes the queue row — never retried
   again.
4. `study_job.status` is wedged at `fetching` with no code path that
   could ever move it.
5. The user is told «Не получилось: техническая проблема», a failure
   nothing in the database recorded.

Now the branch splits on `TERMINAL_STATUSES`, and a non-terminal
redelivery is marked `failed` / `interrupted` with a `finished_at`,
keeping whatever cards were already committed.

**Failed rather than resumed, deliberately.** A resume would re-run a
search or a distill that may already have been paid for, and `/study`'s
candidate loop has no resume point to start from. Plan section 12
already settles a job stopped mid-flight the same way: `failed:cap`
keeps its cards. The daily quota is not refunded, because the money may
genuinely be gone.

A redelivery cannot be a job still running elsewhere: the queue waits
five minutes before returning a claimed row.

**This would be wrong if** research jobs ever became long enough that
losing one to a restart costs real work. At one or two page fetches
they are not.

## Post-4d — the dedupe window had no floor under failed fetches

`_recent_clip_urls` documented a thirty-day window and implemented
`fetched_at IS NULL OR fetched_at >= since`. `fetched_at` is set only
on a *successful* fetch and `study_clip` has no `created_at`, so the
first branch matched **every failed clip ever recorded**. One transient
timeout excluded a URL from every future `/study` permanently, and the
query and its in-memory set grew without bound for the life of the
deployment.

Fixed by joining to the parent `study_job` and bounding the NULL case
by its `created_at` — the timestamp the clip does not have, already
present. A `study_clip.created_at` column would be tidier and needs a
migration; the join costs nothing.

The same DB-clock-versus-injected-clock looseness applies as in
`app/research/sweeps.py`, and for the same reason: in production they
are the same physical clock.

## Post-4d — «без ограничений» left the injection list

It was flagged as borderline when written and is now gone. The phrase
means "unlimited" at least as often as it means a boundary coming off,
and «работайте без ограничений по времени» is ordinary advice.

What made it worth removing rather than tolerating: a hit on this list
does not raise a card's risk, it **drops the card outright**. A false
positive here costs a good card silently, which is the exact failure
mode `docs/decisions.md`'s 4b entry argues these lists must avoid — the
filter you stop trusting is worse than the one that is merely narrow.

The jailbreak vocabulary with one meaning stays: `DAN mode`,
`jailbreak`, `developer mode`, «режим разработчика», `do anything now`.

---

## Post-4d — the `safety_event` blind spot is closed

The 4b entry above promised a 4c revisit. 4c instead made the gap
bigger — `/study` added a search plus a second distill per job — and
this is the revisit.

`ck_safety_event_kind` now admits `distill` and `search` alongside H2's
three. The outcome vocabulary is unchanged and needed no widening: a
distill that will not parse is `parse_fail`, exactly as it is for the
extractor, and a search that comes back with nothing usable is `error`
— the plugin did not do its job, which is what that outcome has always
meant here.

**Two kinds, not one.** They fail differently and for different
reasons, and folding them together would make the one number worth
watching unreadable.

**A search that was never made records nothing.** When the budget is
spent there is no call, and a row saying a search failed when none was
attempted would be the same species of lie as the interrupted-job
branch two entries up.

The rollup is on `/state`, and shown only once there is something to
show — unlike the welfare line. The welfare check runs on ordinary
turns, so a line of zeroes there means it has stopped; research runs
only when asked, so a permanent «0 · 0» would be noise for someone who
never uses `/study` or `/read`.

What this buys, concretely: a distiller that starts returning
unparseable JSON produces `done` jobs with zero cards, over and over,
which is indistinguishable from a run of genuinely unhelpful pages.
`study_job.error_code` carried that per job and nothing aggregated it.
Now `/state` can say the extractor is fine and the distiller has failed
forty times this week.

Recording is staged in the job's own transaction via `record_in`, the
way `app/core/extract.py` stages its own, and never raises —
observability that can fail the job it observes is a worse bug than the
blindness it replaces.

**This would be wrong if** the two kinds turn out to move together in
practice, in which case one `research` kind would read better than two.
Nothing yet suggests that; they have different providers behind them.

---

## W2 — `state_change.source` gains `"web"` with no migration

The web state/proposals panels (`app/web/panels/`) write `user_state`
fields through the same `app/core/commands.py` functions Telegram's
handlers now call, and every one of those writes needs its own
`source` value -- distinct from `"command"` -- so the audit log can
still answer "who changed this" once two transports can make the same
change.

The question worth writing down is whether adding `"web"` needs a
migration. It does not, and the reasoning is a direct reuse of a
pattern this codebase already established twice:

- `app/db/models.py`'s `StateChange.source` column carries **no DB
  CHECK constraint** -- confirmed by `migrations/versions/
  a339f54e49de_create_idle_tables.py`'s own docstring, which states
  outright that the column is deliberately open "the same way
  `spend_ledger.category` is", specifically so a new source value never
  needs a widening migration.
- 6a's undo engine already added `source="undo"` this exact way: widen
  the Python `Source` Literal in `app/core/state.py`, touch no schema.
  `"web"` follows the identical path.

**Decision: widen `app/core/state.py`'s `Source` Literal to include
`"web"`. No migration, no `ALTER TABLE`, no lock.** This is also the
conservative direction given the deploy-lock lessons two ALTERs on hot
tables already taught this project (`telegram_update`'s commit
2cd24c2/068e7e3, and `migrations/env.py`'s `SET LOCAL lock_timeout`
fail-fast as the backstop for whichever online migration eventually
does need to touch one) -- the safest move is simply not needing one
here.

**This would be wrong if** `state_change.source` ever gains a real
CHECK constraint for an unrelated reason (nothing currently proposes
one). That migration would need to enumerate `"web"` alongside the
existing six values, and `migrations/env.py`'s `lock_timeout` guard is
exactly what would make a blocked `ALTER TABLE ... ADD CONSTRAINT`
fail fast rather than hang a deploy, the same backstop already in
place for `telegram_update`.

## Parity pass A — an unconfigured backup stays `status='failed', error_code='not_configured'`

The parity checklist asks for "backup_log status not_configured" when
`BACKUP_AGE_RECIPIENT` and the `BACKUP_S3_*` vars are empty.
`ck_backup_log_status` allows `ok | failed | pruned | purged`, and the
job has always written the pair `status='failed',
error_code='not_configured'` (app/ops/backup.py). `/state` already
reads that as «Бэкап: ⚠️ ошибка», which is what a user with no backups
should see.

**Decision: keep the pair, no migration.** Renaming a status would need
an `ALTER TABLE ... DROP/ADD CONSTRAINT` on `backup_log` for no change
in behaviour. What the pass did change is the part that mattered: a
*partial* or malformed config now lands in the same row instead of
raising. A scheme-less `BACKUP_S3_ENDPOINT` made boto3 raise
`ValueError` before the try block, failing the job with no row. Boot
also logs one `backup partially configured` warning naming the empty
settings (names only, never values) and never refuses to start
(`app/startup.py`'s `warn_partial_backup_config`). Tests:
`tests/test_backup.py`'s partial-config, scheme-less-endpoint and
malformed-recipient cases.

**This would be wrong if** something downstream needs to tell
"never configured" from "configured and failing" by `status` alone.
Today `/state` and the digest read `error_code` for that.

## Phase-6 pass D — a preempted backfill or research run keeps what it already finished

Plan §12 says a preempted idle job ends `skipped:preempted` with "no
partial writes". Five of the seven kinds do exactly that:
- consolidate, reflect and prebrief each write in a single transaction
  opened after the model call and after the in-job preemption
  re-check;
- critique writes nothing but ledger rows and the run summary;
- canary writes nothing but ledger rows and the run summary.

`tests/test_idle_preemption.py` covers all five, mid-run included.

Two kinds keep work on purpose:
- **Backfill** commits one scene per unit. A user message between units
  stops the loop, but the units already done stay done, and the run
  ends `done` with `summary.preempted=true` rather than `skipped`
  (app/core/idle/runner.py). Rolling those back would throw away paid,
  correct summaries of scenes that are over; nothing about the user's
  new message makes them wrong.
- **Research** runs the unchanged /study pipeline. A preemption noticed
  only after it finished keeps its cards, which sit unadopted and need
  /adopt like any other.

A third gap is also left as is. The in-job check and the commit are
two statements, not one locked step, so an update landing between
them is not seen by that run. Closing it would mean holding a lock on
`telegram_update` across the apply, on the table every inbound message
writes to. The window is a few milliseconds, and the worst case is one
idle write that the next turn simply reads.

**This would be wrong if** an idle write could ever change what the
user sees in the turn that preempted it. Today none can: idle writes
summaries, memories marked as idle's own, notebook rows, brief notes
and unadopted cards, and none of them is read mid-turn.

## Phase-6 pass D — restore_check proves the dump is usable, not that it matches production

Plan §9.2 says `scripts/restore_check.py` "asserts the row counts of
the key tables". The script runs from outside production, and this
project's rule is that no tool reads the live database (CLAUDE.md,
docs/claude-access.md), so there is nothing live to compare against.

**Decision:** it asserts what can be checked from the dump alone:
- pg_restore succeeded;
- `user_state` has exactly its one row;
- `alembic_version` is a revision this repo knows.

It prints every table's count for a human to eyeball against `/state`
(`Помню: N записей`).

**This would be wrong if** a content-free count view existed that the
operator could read from the same machine. The `debug` schema could
grow one (`debug.table_counts`) and the script could then compare.

## Phase-6 pass D — the canary never ran: eval built its providers with a removed setting

Production logged `idle run failed event=AttributeError` for the first
canary (2026-09-23). The cause was in `eval/trial.py` and `eval/run.py`:
both built their `OpenRouterProvider`s with
`web_search_max_results=settings.LLM_WEB_SEARCH_MAX_RESULTS`, and
milestone 4a had removed both that setting and that argument. Every
test injects `FakeLLMProvider`s, which skips the construction branch,
so the whole suite stayed green. Three things were broken this way:
- the weekly canary;
- every amendment trial;
- every non-dry `python -m eval.run`, which exited 1 before its first
  call. It could never have exited 3: that refusal is only for a
  judge equal to the chat model.

**Fix:**
- Drop the stale argument.
- `tests/test_eval_providers.py` now builds both provider pairs for real
  (construction makes no network call). It is red without the fix.
- Idle failures now log `where=<module>:<function>:<line>`, the
  innermost frame of this repo's code, next to the exception type.
  They never log the exception message.

**This would be wrong if** a provider construction ever started doing
I/O. The new test would then need a stubbed client, not a skip.

## 8a — the plan's facts, checked against the live sources

Phase-8 plan section 2 lists the facts the design rests on, and asks
that they be re-verified before any code. They were checked against
`obsidian-headless` 0.0.14 (its README and the published `cli.js`,
fetched with `npm pack`) and Railway's docs, on 2026-09-25:

| Fact | Result |
|---|---|
| 0.0.14, Node ≥ 22, UNLICENSED | Confirmed. Still the latest release. Its dependencies are `better-sqlite3` 12.11.1 (native; loads on bookworm glibc) and `commander`. |
| `OBSIDIAN_AUTH_TOKEN`, else `$XDG_CONFIG_HOME/obsidian-headless/auth_token` | Confirmed. The environment wins; the file is read untrimmed. On macOS and Windows the file lives at `~/.obsidian-headless/`. |
| Per-vault state in `…/sync/<vaultId>` | Confirmed: `config.json` and `state.db` there, and a `sync.log`, which is new (below). |
| `--json` disables prompts | Confirmed for `sync-setup`, which then requires `--password`. |
| `--configs ""` disables settings sync | Confirmed: it deletes `allowSpecialFiles`, and the filter treats a missing list as empty. |
| `--file-types` cannot turn attachments off | Confirmed, with a nuance. `""` deletes `allowTypes`, which falls back to `image,audio,pdf,video`. A non-empty list such as `pdf` narrows the set, but there is no way to sync zero types. We never pass the flag. |
| Shared vaults are accepted by `sync-setup` | Confirmed: it searches `vaults` and `shared` alike, by id first and then by name. |
| How `sync-list-remote --json` marks a shared vault | `{"vaults": [{id, name, region}], "shared": [{id, name, region}]}`. Which array an entry is in is the only mark. |
| One volume per service, no replicas with a volume, downtime on redeploy | Confirmed. |
| `<service>.railway.internal`, IPv6-only in legacy environments | Confirmed. Environments created after 2025-10-16 resolve to both families. Binding `::` covers both. |
| `RAILWAY_PUBLIC_DOMAIN` set whenever there is a public domain | Confirmed. There is also a second public endpoint the plan did not name (below). |
| A config-file path for a monorepo service | **Changed**: Config as Code is deprecated (below). |

Four findings changed the plan, and each is its own entry below. The
plan file carries them as rev. 3, each marked *8a*.

## 8a — no `railway.json`: Config as Code is deprecated

The plan had the vault service read `vaultd/railway.json` through a
custom config-file path. Railway's docs now say Config as Code is
deprecated. **New services cannot opt into it**, and existing
`railway.json` files stop being read on 2026-12-01. The replacement,
Infrastructure as Code (`.railway/railway.ts`), is one file for the
whole project. A resource it omits is deleted on apply, so it would
also have to describe the bot and Postgres. It needs the npm `railway`
package, and it is applied with the `railway` CLI, which Claude Code's
guard hook blocks.

**Decision:** the vault service is configured in the dashboard, the way
the bot already is (README → Deploy). `docs/vault-setup.md` lists every
setting: Root Directory `/vaultd`, healthcheck `/healthz`, watch paths,
restart policy, and no domain. Root Directory makes `vaultd/` the
Docker build context, so `vaultd/Dockerfile` is found without a
`RAILWAY_DOCKERFILE_PATH`.

**This would be wrong if** the project moves to Infrastructure as Code
for every service. At that point the vault service belongs in that
file with the rest.

## 8a — vaultd also refuses a TCP proxy

The plan refuses to boot when `RAILWAY_PUBLIC_DOMAIN` is set, because a
public URL would put the API on the internet. A Railway **TCP proxy**
does the same over raw TCP, and sets `RAILWAY_TCP_PROXY_DOMAIN`, not
`RAILWAY_PUBLIC_DOMAIN`. The debug database role is reached exactly
that way (docs/claude-access.md), so this is not hypothetical. vaultd
refuses either variable.

**This would be wrong if** Railway grew a third kind of public endpoint
that sets neither. The bearer token is then the only remaining line.
It is at least 32 characters and compared in constant time.

## 8a — `ob sync` writes its own log, and vaultd truncates it unread

`cli.js` tees everything `ob sync` prints into
`$XDG_CONFIG_HOME/obsidian-headless/sync/<vault id>/sync.log`,
append-only and never rotated. The plan discards `ob`'s stdout and
stderr so that file names never reach Railway's logs. This file is on
the volume, so it is not a log leak. But it holds the same file names,
and it grows forever on a volume sized for the vault.

**Decision:** the supervisor truncates every `sync.log` under that
directory before each start of the child, without reading it, and
skips anything that is not a regular file. `ob sync`'s stdout and
stderr go straight to `/dev/null`, never through a pipe. That is the
stronger form of the plan's "drained and discarded": nothing in vaultd
ever holds those bytes.

**This would be wrong if** `sync.log` turned out to be the only record
of a sync failure worth debugging. It would then need a size cap rather
than truncation, and it would still never be read by anything that
logs.

## 8a — `ob` children get an allowlisted environment

vaultd's own environment holds `VAULT_API_TOKEN`, the Obsidian token
and the end-to-end password. None of that needs to reach `ob` except
the Obsidian token. Every `ob` process gets exactly `PATH`, `HOME`,
`XDG_CONFIG_HOME` and `OBSIDIAN_AUTH_TOKEN`. `sync-setup` takes the
password as an argument (next entry). `ob sync` needs no password at
all, because setup stored the derived key in `config.json`.

**This would be wrong if** a future `ob` needed another variable. It
would then fail loudly at boot, which is the direction we want.

## 8a — the end-to-end password is on `sync-setup`'s argv

`ob` accepts the vault's end-to-end password only as `--password` on
the command line, or at an interactive prompt that `--json` disables.
While `sync-setup` runs, the password is visible in
`/proc/<pid>/cmdline` to any process in the same container. Every
command is started as an argv list, never through a shell. vaultd runs
`sync-setup` only when `VAULT_PATH` is not yet linked, which is the
first boot, so the window is one command's lifetime, once.

The exposure is real but narrow. The container runs only vaultd and
`ob`, both of which hold the password anyway, and nothing in it logs
argv. It is recorded rather than worked around because the
alternatives are worse: feeding a TTY to an interactive prompt, or
patching `cli.js`, which is UNLICENSED and must not be vendored.

**This would be wrong if** anything else ever ran in the vault
service's container, or if `ob` grew a `--password-file` or an
environment variable, which we would then use.

## 8a — refuse a vault path linked to a different vault

`sync-list-local` reports which remote vault each local path is linked
to. If `VAULT_PATH` is already linked, and not to the vault
`OBSIDIAN_VAULT` names, vaultd refuses to start instead of syncing the
old one. Changing `OBSIDIAN_VAULT` is then a deliberate act, done by
clearing the sync state on the volume (docs/vault-setup.md), and never
a silent merge of two vaults' files.

`OBSIDIAN_VAULT` is resolved to an id before `sync-setup` runs, and the
id is what is passed. `ob`'s own lookup also searches shared vaults by
name, and it never gets the chance. A name that matches both your own
vault and a shared one is refused rather than guessed at.

**This would be wrong if** two vaults ever legitimately shared one
path. They cannot: ob keeps one link per path.

## 8a — PyYAML, which 3e avoided

Phase 3e kept PyYAML out: the eval cases were the only YAML in sight,
and `tomllib` served them from the standard library (README, "Two
deviations from section 9"). Phase 8 is different in kind, not degree.
Obsidian properties *are* YAML, written by Obsidian, by Bases and by
Sync's merge. A hand-written parser for them would be a security
boundary built on a subset guessed from examples.

**Decision:** PyYAML goes into both projects, and is used only through
one loader: `SafeLoader` subclassed to raise on anchors, aliases and
duplicate keys, fed at most 4 KB. vaultd has it from 8a for the opt-in
check. The bot declares it in 8a (approved for this milestone) and first
imports it in 8b. No `python-frontmatter`: splitting a fence is a dozen
lines (`vaultd/vaultd/frontmatter.py`). The two projects each keep
their own loader, by the independence rule.

**This would be wrong if** PyYAML's `SafeLoader` ever constructed
arbitrary objects. It does not, and a `!!python/…` tag is one of
vaultd's refusal tests.

## 8a — vaultd's residual write race

Every write and delete is compare-and-swap, serialised through one
`asyncio.Lock`, and the lock only orders vaultd's own requests. `ob` is
another process. An update writes and fsyncs a temp file, re-checks
the hash of the bytes on disk, then `os.replace`s. If `ob` writes the
file in the microseconds between the re-check and the replace, `ob`'s
write is lost to ours.

This is not papered over with a file lock `ob` would never take. It is
closed by the next pass instead: the manifest then shows the hash of
what is on disk, which is ours, and Sync still holds your version in
its history. The window is microseconds; a human edit arriving through
Sync lands seconds apart from anything the bot does. Create-only
writes have no such window, because `os.link` fails atomically if the
target exists (a test injects exactly that race).

**This would be wrong if** `ob` ever held a lock that another process
could take. We would then take it.

## 8a — what vaultd's status codes promise

- **400:** the request is malformed (the path or the body). It says
  nothing about the vault.
- **403:** a write, delete or purge on a path outside the writable set,
  or one that crosses a symlink. The bot never produces one on purpose.
- **404:** every read refusal. A missing note, a note without
  `anchor: read`, a dot-folder, a symlink and a folder all get the same
  status and the same body, so a caller cannot probe for what it may
  not see.
- **412:** compare-and-swap lost.
- **422:** a file in `Anchor/Memory/` or `Anchor/Journal/` that is not
  UTF-8. It is in Anchor's scope, so it is listed and it exists, but it
  cannot be returned as text. 8c quarantines it. A note that is not
  UTF-8 is never opted in, and so is simply absent.

Symlinks are refused by walking each path one component at a time with
`O_NOFOLLOW`, relative to the parent's descriptor, rather than by an
`lstat` check followed by an `open`. `ob` cannot swap a folder for a
link between the two.

**This would be wrong if** the bot needed to tell "missing" from "not
opted in". It must not, which is why they are the same.

## 8a — limits that are constants, not settings

vaultd's note size cap (`NOTE_MAX_BYTES`, 200 000), the 4 KB
frontmatter cap, the 64 KB body cap, the restart backoff, and the bot's
client timeout (5 s) and response cap (8 MB) are all code constants.
Each of them protects the boundary, and a deploy must not be able to
widen it by pasting a variable. The plan's `VAULT_NOTE_MAX_BYTES` (8d)
is a bot setting, and it can only narrow what vaultd already lists.

The bot gets only the settings 8a reads: `VAULT_MODE`, `VAULT_URL` and
`VAULT_API_TOKEN`. The plan's grace, warmup, caps and notes settings
arrive with the milestones that read them, so no setting exists that
does nothing.

**This would be wrong if** a real vault had notes larger than 200 KB
that should be searchable. Split them; the chunker would cut them up
anyway.

## 8a — `mirror` and `sync` are accepted, and act as `status`

`check_runtime_settings` accepts all four modes, so a `VAULT_MODE=sync`
set ahead of a deploy does not take chat down. Plan section 2's failure
isolation says the vault never takes the bot down. Until 8b and 8c
ship, `mirror` and `sync` do exactly what `status` does, and `/vault`
says the mode is not in this build yet. `tests/test_vault_commands.py`
pins, for every mode, that nothing beyond `GET /v1/status` is ever
requested, and that the heartbeat queues no vault job.

The token and URL are checked whenever the mode is not `off` *or* a
token is set. From 8b, a set token alone lets `/delete` reach the vault
service in any mode.

**This would be wrong if** accepting an unimplemented mode ever hid a
misconfiguration. `/vault` names it, which is the place you would look.

## 8a — `/vault` and `/state` probe the service and remember the answer

In `status` mode no sync pass runs, so nothing else would notice the
vault service going away. Both commands make one `GET /v1/status` and
record the answer in `vault_status`: `last_ok_at` and
`ob_running_since` on success, `last_unavailable_at` on failure. That
is what lets `/state` say «нет связи с 14:05». In `off` they make no
request and write nothing.

`/state` gained one state the plan's list lacks, «синхронизация
остановлена». vaultd answers but `ob` is not running, which is neither
«ок» nor «нет связи». «удаление файлов ожидает» arrives with 8b's
`vault_purge`.

**This would be wrong if** `/state`'s latency mattered more than the
line. A hung vault service costs it up to the client's 5 s timeout. A
refused connection costs nothing.

## 8a — the CHECKs the schema states

Every invariant plan section 6 writes as a comment is a constraint:

- `ck_vault_file_role_columns`: a fact has no date; a journal day has
  a date and no memory; a note has neither.
- `ck_vault_file_held_has_hold`: `held` if and only if there is a hold.
- `ck_vault_file_reason_code`: a reason is a snake_case code of at most
  40 characters, never text. The plan says "a code from errors.py,
  never free text". The list itself is 8c's, so the shape is what 8a
  can pin.
- `ck_vault_file_path_relative`: non-empty, no leading `/`, no
  backslash. The same rule vaultd enforces, stated again where the
  paths are stored.
- `ck_vault_hold_payload_object`, plus one per kind:
  `{"file_ids": [...]}` for `mass_delete`, and all four keys for
  `rule`. Both are wrapped in `coalesce`, because a CHECK that
  evaluates to NULL passes, and a missing key makes these NULL. A test
  caught exactly that.
- `ck_vault_status_singleton` and `ck_vault_status_forgets_array`.
- `ck_user_state_vault_epoch`: six base32 characters.

The epoch has a Python default (`secrets`) rather than a server one.
Postgres cannot draw from a CSPRNG without an extension, and every
insert of that row goes through SQLAlchemy. The migration draws its
own epoch for the existing row, so it does not import app code.

**This would be wrong if** 8c's reason codes needed digits or more than
40 characters. Widening a CHECK is one migration.

## 8b — mirror records edits and applies none

Plan section 7 has mirror's ingest update `disk_sha256` and nothing
else. 8b implements exactly that, and no more. It resolves no
identities, creates no fact from a file, runs no deletions (section 7.2
is skipped in mirror) and holds nothing. So a fact file you delete or
rename is not recreated: its compare-and-swap update fails and is
skipped, until 8c decides what the deletion means. `sync` mode behaves
exactly like `mirror` until 8c ships. An AST test pins that nothing in
`app/vault/` so much as names a function that changes memory.

The fact file's callout tells the truth for the mode it is written in.
In mirror it says edits are not applied, not plan 4.1's «Меняй `fact`…».
8c changes the text back, and that rewrite of every fact file is paced
by the write cap like any other.

**This would be wrong if** someone ran mirror for weeks expecting edits
to stick. `/vault` says in plain words that they do not.

## 8b — your properties are carried over verbatim

Plan 4.4 says a re-render writes Anchor's keys first, then "your keys
in their original order and form". "Form" is taken literally: the
composer's line marks cut each non-Anchor top-level key out of the
source text, comments and flow style included, and the lines are
appended unchanged. Re-dumping them through PyYAML would re-quote and
re-flow them. The file would then look as if Anchor had edited your
properties, which it must not.

A file whose frontmatter fails the strict loader (duplicate keys from a
Sync merge, an alias, a syntax error) is **not rewritten**. It is
quarantined `bad_yaml`, and the quarantine lifts the next time the
file's hash changes. Rewriting it would silently drop whatever the
loader could not read.

**This would be wrong if** users routinely left broken YAML in fact
files. Such a file then stays frozen until fixed. The problem list
that shows it arrives in 8c; until then `debug.vault_file` shows the
reason code.

## 8b — crashes on either side of the PUT converge

A new fact's row is committed before the create-only PUT (plan 7.3).
Two crash points follow:

- **Before the PUT:** a row with no hash and no file. The next pass
  creates the file.
- **After the PUT, before the hash is recorded:** a row with no hash and
  a file. The next pass records the manifest's hash first. The render
  then finds its own content already on disk and adopts it, with no
  write and no `name_taken`.

Both are tests.

`name_taken` is reserved for a create-only PUT that finds a file Anchor
has no record of. With the epoch in every name, that is practically
impossible.

## 8b — a welfare day keeps its check-in note out of the vault

A check-in note is stored before the welfare check runs on it. If it
trips the check, the message is retagged `welfare`, but `checkin.note`
keeps the text. Rendering «Заметка: …» would put welfare text in the
vault, which plan section 10 forbids.

**Decision (yours):** the day file omits the note whenever a
`message.kind='welfare'` row falls on the check-in's local date or the
day after, since a check-in can be answered past midnight. There is no
schema change and no change to Phase 2. A test renders a welfare day
and asserts that the text appears in no file.

**This would be wrong if** an unrelated note on a welfare day mattered
in the vault. It is still in the database and in `/export`.

## 8b — `/delete`'s vault line appears only with a vault

«Файлы Anchor в хранилище тоже удалятся. Obsidian Sync хранит их в
истории версий ещё до месяца, зашифрованными.» is added to the
confirmation only when `VAULT_API_TOKEN` is set. A vault line with no
vault configured would itself be a false statement. The retention
figure is Standard's month (plan §18.3, your answer). On Plus it would
read «до года».

## 8b — `/forget` of a corrected fact, pending §18.1

8b keeps today's `/forget`: deleting a corrected fact's head
reactivates its predecessor (`test_forget_the_head_of_a_chain_clears_the_pointer`).
In the vault, the head's file is deleted and the predecessor gets a
file of its own on the next pass. Whether `/forget` should forget the
whole lineage, as deleting a file will in 8c, is plan §18.1, to be
settled before 8c.

## 8b — technique sources, and the lowest card id

A technique's file quotes the card it came from, and names the domain
from the card's clip. The card still points at the row originally
adopted, so every id in the lineage is searched. When several cards
match (adoption falls back to a near-duplicate memory, so two cards can
land on one fact), the lowest card id wins, so the file is
deterministic.

The whole memory table and the linked cards are read in two queries
per pass, and lineages are walked in Python. That avoids 2 × N queries
a minute for N facts; a personal memory is hundreds of rows, not
millions.

## 8b on main — the vault lands on `main` as Phase 8

The vault was written as "Phase 5" on a branch that stopped at Phase 4,
while `main` went on to ship its own Phase 5 (personality) and Phase 6
(idle learning), and reserved Phase 7 for trackers and devices. Merging
it therefore renumbered it: the plan is `anchor-phase8-plan.md`, the
milestones are 8a–8d, and every reference in code, docs and tests
followed. Only text the vault itself added was renumbered; `main`'s own
"5a"–"5e" labels are its Phase 5 and stay as they are.

Three things on `main` changed what the vault had assumed. Each has an
entry below.

## 8b on main — `/forget` keeps `main`'s protection, not a `forgotten` card

The vault plan fixed a phase-4 bug (a `/forget` of an adopted technique
raised on `study_card`'s foreign key) by adding a card status
`forgotten`. `main` had already fixed the same bug another way:
`hard_delete` refuses to delete a lineage head that an adopted card
points at (`FORGET_PROTECTED`), and relinks cards to the successor
otherwise.

**Decision (yours): `main`'s protection stays.** The `forgotten` status,
its migration and its `hard_delete` change were dropped in the merge. A
protected fact keeps its memory, so it keeps its vault file, which a
test pins. Plan §17's "`/forget` works on an adopted technique" is
superseded by that protection, and 8c's `forget_lineage` must honour it
rather than bypass it.

## 8b on main — idle consolidation moves memory behind the vault's back

`write_memory` moves `vault_file.memory_id` to a lineage's new head in
the same transaction. Phase 6's idle consolidation
(`app/core/idle/consolidate.py`) does not call it. It inserts the merged
fact and sets `superseded_by` on the originals directly, and can merge
two originals into one. Its undo (`app/core/idle/undo.py`) deletes the
merged row and reactivates the originals, also directly. Left alone,
that would leave a file stuck on a superseded row and create a second
file for the head.

**Decision:** the sync pass repairs this itself before rendering.
`_follow_heads` walks each fact row's memory to the head of its chain.
The oldest row to reach a head keeps it, and any later row landing on
the same head is treated as forgotten, so its file is deleted by
compare-and-swap. After an undo, the merged row's deletion nulls its
file row (`ON DELETE SET NULL`), which deletes that file, and the
reactivated originals get files of their own. A test runs the merge and
the undo end to end.

The fix lives in `app/vault/`, not in `consolidate.py`, on purpose: the
vault must stay correct whoever writes memory, and Phase 6's isolation
rules keep idle modules from importing `app.core.memory`.

**This would be wrong if** a merge should keep *both* originals'
histories in one file's `## Раньше`. Today it shows the chain through
the lowest-id original only.

## 8b on main — the vault service's config and main's root `railway.json`

`main` deploys the bot through a root `railway.json` (Config as Code),
which Railway has deprecated. The vault service does not use it: its
Root Directory is `/vaultd`, so Railway reads neither that file nor
any other config file for it. Its settings are in the dashboard, as
`docs/vault-setup.md` describes (8a's decision). The bot's own
`railway.json` stops being read on 2026-12-01, and moving the bot off
it is outside the vault's scope.

## 8a fix — vaultd listens on IPv4 and IPv6

The first deploy of the `vault` service booted cleanly (`boot done`,
`ob sync started`) and then failed Railway's healthcheck for five
minutes: "service unavailable" on every attempt. vaultd bound its API to
`::` alone, on the belief that a Linux socket on `::` also accepts IPv4.
A raw socket does, but asyncio's `create_server` sets `IPV6_V6ONLY` on
every IPv6 listener, so the API was reachable over IPv6 only, and
Railway's healthcheck connects over IPv4.

**Decision:** bind with `host=None`, which gives one listener per
address family the host has: `0.0.0.0` for the healthcheck, `::` for
private DNS in legacy (IPv6-only) environments. `tests/test_listen.py`
fetches `/healthz` over `127.0.0.1`, and over `::1` where the host has
IPv6. The API stays private either way: the service has no public
domain or TCP proxy, which boot still refuses.

## 8e — unreadable properties hide the note

8a had two outcomes for a note's properties: opted in or not. 8e adds
folder rules, and with them a third question: may a folder rule decide
this note? vaultd's `note_mark` answers `none` (yes) only when the note
plainly says nothing: no leading fence, an empty block, or a mapping
with no `anchor` key. Everything that *might* have said something is
`unknown`, which hides the note whatever its folder says: an unclosed
fence, a block over 4 KB, a byte-order mark before the fence, an alias,
a duplicate key, a non-mapping, a non-string `anchor`, or a file that is
not UTF-8. So does any value other than the four Anchor knows
(`never`, `personal`, `knowledge`, `read`), including `Knowledge`, a
typo and `settings`.

**Why:** a Sync merge that leaves `anchor: never` twice must not turn a
note in a knowledge folder visible. Reading "I cannot tell" as "no
opinion" would do exactly that.

**This would be wrong if** people routinely kept broken YAML in notes
they want read. `/vault` counts them as «неизвестная метка», so they are
visible as a number, and fixing the note brings it back.

## 8e — a conflict is a property looser than its folder

The plan says `/vault` counts each disagreement. `effective_class`
counts one only when the property is *looser* than the folder: a
`knowledge` note in a personal or never folder, or a `personal` note in
a never folder. That is the case where your own word was not followed.
A stricter property, `anchor: never` on one note inside a knowledge
folder, is the ordinary way to carve out an exception, and counting
those would bury real conflicts. A legacy `anchor: read` in a never
folder counts as a conflict too, since it reads as personal.

## 8e — the settings file

`Anchor/settings.md` fails closed, as the plan says, and 8e decides what
"unusable" covers beyond the plan's list:

- **An unknown key is invalid.** A typo like `never_folder:` would
  otherwise silently drop every never-rule, which is the one failure
  the fail-closed rule exists to prevent.
- **A list that is `null`** (`never_folders:` with nothing after it) is
  invalid, not empty. The template writes `[]`.
- **A folder entry** must be a non-empty string with no leading or
  trailing `/`, no backslash or NUL, no empty, `.`, `..` or dot-segment.
  A trailing slash is refused rather than trimmed: every silent repair
  of the file is a guess about what you meant.
- **A symlink, a folder or a non-regular file** at that path is invalid,
  not absent. The same goes for a read error.
- **The file is never listed, never served and never writable.**
  `GET /v1/file` refuses it by name, before reading it.

The shipped template (`docs/vault/settings.md`) is parsed by a vaultd
test, so its Russian comments cannot push it past the 4 KB frontmatter
cap unnoticed.

## 8e — never-rules match in any case

Folder names are compared segment by segment after NFC. `never_folders`
are also compared casefolded; `personal_folders` and `knowledge_folders`
are not. A never-rule typed as `life/diary` for a folder named
`Life/Diary` should still hide the diary, and one that matches too much
only hides more. A readable rule that matched in any case could reveal
a folder you did not name.

## 8e — the migration refuses rather than guesses

`c3e8f5a1d2b6` stops if `vault_chunk` has rows, as the plan asks, and
also if any `vault_file` row has `role='note'`. The new CHECK requires
every note row to carry a class, and 8e will not pick one. Nothing on
`main` writes either (checked in code, not production data), so both
refusals should never fire. If one does, the message names the table,
and deleting the rows by hand is safe: both are derived from the vault,
and 8d rebuilds them. `tests/test_vault_notes_migration.py` runs the
migration on its own throwaway database and proves both refusals.

The debug views follow the plan: `debug.note_chunk_personal` and
`debug.note_chunk_knowledge` carry `id, file_id, ord` and the text's
length, without the heading length `debug.vault_chunk` had, and
`debug.vault_file` gains `note_class`. `debug.user_state` does not gain
`notes_consent`: the plan did not ask for it, and that view never
carried the vault's columns.

## 8e — consent is checked inside the access modules

The plan puts consent at the callers: 8d indexes and retrieves only
while `notes_consent` is on. The access modules check it as well.
`search` joins `user_state` and returns nothing without consent, in the
same query. `replace_chunks` raises `NotesConsentOff`. 8d's callers will
still check first. This is the floor under them, so a caller that
forgets cannot read or write a note without consent.

`/vault notes off` deletes the note file rows, which cascade to both
chunk tables, and sets the flag, in one transaction. It never has to
touch a chunk table by name.

## 8e — `search` has no rank threshold yet

`search` returns every match, best first, up to `limit`. The plan's
per-class thresholds (`PERSONAL_MIN_RANK`, `KNOWLEDGE_MIN_RANK`) are to
be measured in 8d, as 2b measured trigram retrieval. Inventing a number
now would be a threshold nobody measured. Nothing calls `search` in 8e.

## 8e — the per-class settings exist before anything reads them

8a's rule was that no setting exists that does nothing.
`VAULT_KNOWLEDGE_ENABLED`, `VAULT_PERSONAL_ENABLED` and the two
`*_IN_PROMPT` caps break it, because the 8e plan asks for them to be
declared and validated now. Both switches default to off. The caps are
bounded by a constant (`NOTES_IN_PROMPT_MAX`, 5), checked at boot in
every mode, so a pasted variable cannot flood a prompt.

## 8e — `/vault` reads the manifest, in `status` mode too

8a pinned that `status` mode makes one request, `GET /v1/status`. With
notes consent on, `/vault` now also makes one `GET /v1/manifest` to
count notes by class, in every mode but `off`. Without it you could not
check your classification until mirroring. The paths in the response
are counted and dropped in `app/vault/status.py`, and nothing is
logged but an error code. With consent off, no manifest is requested.
`/state` is unchanged. `tests/test_vault_commands.py` pins both cases
for every mode.

**This would be wrong if** a large manifest made `/vault` slow. It is
one request under the client's 5 s timeout, and a failure only drops
the notes line.

## 8e — `/vault`'s labels are split

The plan's line put conflicts, `anchor: read` and unknown values all
under «не прочитано». Only an unknown value is actually unread: a
conflicting note and a legacy note are both read, as personal. The line
therefore says «проверить: конфликт N, anchor: read N» and «не
прочитано: неизвестная метка N». Only nonzero parts are shown. The reply
to `/vault notes on` is the plan's text without its Markdown backticks,
since every reply is plain text.

## 8e — an old vaultd behind a new bot fails closed

The bot now requires `class` on every note entry and a valid `summary`
on every manifest. If the bot deploys before the vault service, each
manifest is a protocol error (`bad_response`) until vaultd redeploys:
mirror passes fail and retry each minute, and `/vault` drops its notes
line. That is the intended direction. The bot never falls back to
treating an unclassified note as readable. Both deploy from the same
merge, since the vault's watch path `/vaultd/**` matches.

## 8e — the manifest cache kept only what it listed

vaultd's manifest evicted from its cache every path it did not list, so
a note that was not opted in was re-read and re-hashed on every scan.
8e needs the opposite: a settings edit must reclassify notes without
re-reading them. So every note the scan considered stays cached, listed
or not. `test_editing_settings_reclassifies_without_rereading_notes`
checks `last_reads == 0` across four settings changes.

## 8e — `docs/privacy.md` was a line short

The English privacy note had no line for the planner, which
`PRIVACY_TEXT` has carried since P4. 8e adds that line along with the
notes line, and `tests/test_privacy.py` now checks that the two have the
same number of lines and that both mention Obsidian. `PRIVACY_TEXT` is
at 10 lines, the ceiling its own test allows.

## C1 — one column, not two

The connector plan's C1 adds `access_grant.client` and `connection_id`.
`connection_id` references `oauth_connection`, which C2 creates, so C1
adds only `client` (default `grok`, CHECK `in ('grok','claude')`) and
the pairing CHECK `(client = 'grok') = (token_sha256 is not null)`.
`token_sha256` stays NOT NULL, so the two checks together refuse any
`claude` row. C2 makes the token nullable in the same migration that
adds `connection_id` and its check. Until then the database, not the
code, keeps a Claude grant from existing. `debug.access_grant` gains
`client`.

**This would be wrong if** C2 needed Claude rows before its own
migration, which it cannot: a window needs a connection.

## C1 — the grant lookup reads only Grok's rows

`grants.find_active_grant` now also filters `client = 'grok'`. No other
row can exist yet, so it changes nothing today. From C2 it means a token
hash can never open a Claude window, whatever ends up in
`token_sha256`.

## C1 — an unknown tool stays a protocol error

The shared core takes the refusal as a parameter: Grok's JSON-RPC
`-32602`, or Claude's tool result with `isError: true` and «Доступ
закрыт. Открой его в Telegram: /claude». The parameter covers only a
**known** tool that the reader may not use now (no window, or a scope
outside it). An unknown tool name or malformed arguments are a `-32602`
for both clients, because that is what MCP specifies for them and there
is no window a user could open to fix it. `tools/list` follows
`Reader.listed` and `tools/call` follows `Reader.grant`, so a
connection's cached tool list and its current window may differ, as
plan section 6.2 requires.

## C1 — `access_grant` stays out of `/export`

Plan section 7 says `access_grant` "stays exported, now with `client`
and `connection_id`". It never was: `tests/test_export.py` has listed
it in `NOT_EXPORTED` ("token hashes and grant bookkeeping, not user
data") since Grok shipped. C1 keeps it out, which keeps less, and
changes nothing in the export. C2 adds the `oauth_*` tables to the same
list.

## C2 dry run — a probe, because only a server can observe claude.ai

C2 may not write OAuth code before plan section 11 is answered, and
claude.ai's discovery, registration and authorize requests can only be
seen by a public server that answers them. `app/web/oauth_probe.py` is
that server. It sits behind `CLAUDE_OAUTH_PROBE` (off | both | cimd |
dcr) and is refusing by construction:
- `/mcp/claude` is always 401;
- authorize shows a page and never redirects, so no code, token or connection can exist;
- it writes no row and sends no Telegram message;
- its imports are pinned to aiohttp and `app.config`.

Two deviations from the approved sketch, both toward seeing more without granting more:
- **DCR registration answers 201** with one fixed public `client_id` (`anchor-dry-run`, stored nowhere) instead of refusing. Otherwise claude.ai would stop before authorize, and the DCR run could not show its `resource` and PKCE shapes.
- **`/.well-known/...` paths the probe does not serve are logged by path,** so a discovery URL we did not expect still shows up.

It is registered before Grok's `/mcp/{token}`, which would otherwise match `/mcp/claude` and 404 it. C2 replaces the module and removes the setting.

**This would be wrong if** claude.ai behaved differently against a
server that later issues tokens. The registration, callback and
`resource` shapes are request-side and do not depend on that. The
token-side questions (§11.4, §11.5) are deferred to C2's manual check
for exactly this reason.

## C2 dry run — a client_id URL is logged only on claude.ai

The probe logs a `client_id` URL's host always, and its path only when
the host is `claude.ai` or `claude.com` (the user's choice). That
document is Anthropic's public client metadata, and C2 must pin its
exact URL on an allowlist. A `client_id` on any other host is a
stranger's input: its host is logged, its path is not. No other value
reaches a log line: parameters and headers appear by name, and
everything else is a closed vocabulary or a boolean.
`tests/test_research_isolation.py` forbids a log key named for free
text ("path" among them), so `client_id_path` is a named, commented
exemption there. It goes when C2 removes the probe.

## C2 prep — Railway's http log is where the Grok token leaks (§11.7)

The plan asked whether `mcp__Railway__http-requests` reveals request
paths. From the tool's own description, it does not: it returns counts
per status class, and `path` is only an input filter. It stays allowed.
The leak is next to it. `mcp__Railway__get-logs` with `types: ["http"]`
returns Railway's edge log: method, **path**, status and timing
(docs.railway.com/cli/logs#http-logs). That tool was allowed, so Grok's
`/mcp/<token>` was readable from Claude Code. The guard hook now blocks
`get-logs` whenever `types` includes `http` (any case, or a malformed
`types`). Nothing was called to confirm this; the finding is from the
documentation, as the plan asked.

## C2 prep — the guard blocks every "anchor…" server

The plan's pattern `(?i)^mcp__(?:claude_ai_)?anchor\w*?__` also matches
an unrelated server named, say, `anchorage`. That is kept: over-blocking
a stranger costs a denied call, while missing a renamed Anchor costs
the dialogs. Any `mcp__*` tool whose own name is one of Anchor's read
tools is blocked too, whatever its server is called.

## C2 — the dry run's answers (constants)

The probe (`app/web/oauth_probe.py`) was run against claude.ai twice on
2026-09-25, advertising `both` and then `cimd`. The runs matched request
for request (docs/claude-connector-dry-run.md, "Results"). What C2
builds on:

- **Registration is CIMD, and only CIMD.**
  - The authorization-server metadata advertises `client_id_metadata_document_supported: true`. It has no `registration_endpoint`, and there is no `/oauth/register`.
  - claude.ai chose CIMD even when DCR was offered, and works with CIMD alone. So the plan's §7 `oauth_client` table is not created.
- **`CLIENT_ID = "https://claude.ai/oauth/mcp-oauth-client-metadata"`** is the only accepted `client_id`.
  - The document is never fetched: no request is made to a URL a client supplied.
  - The `redirect_uris` it would list are pinned in code instead.
  - If claude.ai moves the document, connecting fails closed with «Клиент не распознан».
- **`REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"`,** matched byte for byte (§11.2 confirmed).
- **`resource` is sent, and sent exactly** (§11.3). The plan's tolerance for scheme or host case and for a trailing slash stays; it costs nothing.
- **`scope` arrives as `anchor.read`, and `state` is always present.**
- **Discovery** starts from the 401's `resource_metadata` (the path-suffixed RFC 9728 URL), then the root RFC 8414 document. No OpenID discovery was attempted. The root RFC 9728 copy was never requested; C2 serves it anyway, as plan §4 says.
- **Authorize repeats.** The same authorize request arrived five times within three minutes, from reloads and repeated Connect presses. Each creates its own pending request, under the caps of 5 per IP and 20 in total. A later request never invalidates an earlier one; only the one whose code is typed is approved.
- **There is no per-surface switch (§11.6).** claude.ai offers no way to keep a connector out of Claude Code sessions or routines. The fences are the ones plan §6.3 names: this repo's guard, short single windows, and a Telegram notice for every read.
- **Not answered by the dry run** (§11.4 retries, §11.5 `isError` and 401 mid-chat): these stay in C2's manual check, before `CLAUDE_ACCESS_ENABLED` stays on.

**This would be wrong if** claude.ai changed its metadata URL or its callback. Both would then fail closed, visibly, at connect time, and the fix would be one constant.

