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

*Superseded by "8c — `/forget` forgets the whole lineage" below.*

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

## C2 — the probe is gone, and what replaced its choices

C2 deletes `app/web/oauth_probe.py`, `CLAUDE_OAUTH_PROBE`, the probe's
log keys and the `client_id_path` exemption in
`tests/test_research_isolation.py`. The real server keeps one shape
from the probe: it logs a route and an outcome, never a value.

## C2 — no `oauth_client` table, and `oauth_token.request_id` is SET NULL

Registration is CIMD only, and the one accepted client id is a
constant, so the plan's DCR table is not created. The plan made
`oauth_token.request_id` cascade on delete, but requests are swept after
a day and tokens live 30. With CASCADE, the retention sweep would have
deleted live tokens. It is `ON DELETE SET NULL`: replay revocation only
needs the request during the minutes after its code is used.

## C2 — the token endpoint accepts an absent `resource` or `client_id`

At the token endpoint, `resource` may be absent: the code, or the
refresh token, is already bound to it. If present, it must name
`/mcp/claude`. On refresh, `client_id` may likewise be absent, and
must match if present. The code exchange always requires it. Refusing
the absent case would gain nothing, since the binding is already
stored, and could break claude.ai's refresh, which the dry run could
not observe. A *mismatch* is refused everywhere.

## C2 — the reply to `/claude connect` says «Подтверждено», not «Подключено»

The plan's reply was «Подключено. Старое подключение (если было)
закрыто.» But approving a code does not yet create the connection:
claude.ai must still collect the code and exchange it, within 60
seconds. The old connection closes at that exchange. The reply
therefore says «Подтверждено. Вернись в браузер: claude.ai завершит
подключение сам, а старое подключение (если было) закроется».

## C2 — one browser, one binding cookie

The dry run saw the same authorize request arrive five times in three
minutes, from reloads and repeated Connect presses. If each request set
a fresh `__Host-anchor_oauth` cookie, the older waiting pages in that
browser would lose theirs and stop working. A request that arrives with
a well-formed cookie reuses it: the pending entry stores its hash, and
the cookie is not reset. A different browser still gets its own cookie
and cannot poll another browser's request.

## C2 — the per-address cap uses the last `X-Forwarded-For` entry

Railway's edge appends the real peer to `X-Forwarded-For`. Earlier
entries are whatever the client sent. The per-address cap of 5 pending
requests therefore counts the last entry; a spoofed leading entry
changes nothing (tested). The total cap of 20 holds regardless.
Someone who fills it can only delay a connection by 10 minutes.

## C2 — the `/claude connect` lockout lives in memory

Five wrong codes in an hour lock `/claude connect` for an hour. The
counter lives in the pending store's memory. A restart resets it, which
is the web passphrase limiter's trade-off (app/web/ratelimit.py). A
guesser cannot trigger a restart, and after one there are no pending
requests left to guess anyway.

## C2 — `/revoke` keeps Grok's text when only Grok was open

`/revoke` now closes Claude windows too. When it closed Grok grants
only, it answers with Grok's existing text, which also says the link is
dead. Otherwise it answers «Доступ закрыт: Grok (n), Claude (m).».
This keeps `tests/test_grok_access.py` unedited, and each reply says
what actually happened. The Grok picker's «Сейчас открыто» now lists
Grok grants only.

## C2 — the flag off revokes at startup

"Turning the flag off revokes every connection" is implemented in
`run_startup_tasks`: when `CLAUDE_ACCESS_ENABLED` is false, it revokes
every connection and token and closes every Claude window. A flag
change needs a redeploy anyway, so the revocation happens before any
request is served. Turning the flag back on then finds nothing to
revive (tested).

## C2 — one PendingStore, shared

As with the web UI's `CodeStore`, `main()` builds one `PendingStore`
and hands it to `build_router` (for `/claude connect` and `/delete`)
and to `build_webhook_app` (for `/oauth/authorize`). `/delete` now
clears it, alongside the WebHub. Before, the web `CodeStore` was
cleared only by `/weblogout`; that is noted here and left as it was,
outside C2's scope.

## C2 fix — one PendingStore, really (and the code on a Russian keyboard)

In production, `/claude connect <code>` always answered «Код не найден
или устарел». `build_webhook_app` registered the authorization server
with `claude_pending or PendingStore(clock)`. Because `PendingStore`
has `__len__`, an empty store is falsy, so at startup `or` built a second
store. `/oauth/authorize` filled that second store, and `/claude connect`
searched the one `main()` gave the dispatcher. The tests missed it
because they handed one store to both sides directly.

The fix has three parts:
- an explicit `is not None`;
- `PendingStore.__bool__` returns True, so an empty store can never be falsy again;
- `tests/test_claude_wiring.py` builds both sides through `main.build_webhook_app` and `main.build_dispatcher`, from one empty store, exactly as `main()` does.

`match()` also maps the Cyrillic twins of the code's letters (А В Е К
М Н Р С Т У Х) to Latin before comparing. A code typed on a Russian
layout is otherwise unmatchable and counts toward the lockout. It is
still one exact, constant-time match against a live code. A Cyrillic
letter that is not a look-alike still matches nothing.

## C2 — first contact: the connector surfaced in Claude Code

The first real connection (connection #1, 2026-09-25) was named `anc`
in claude.ai. Within seconds, the Claude Code session that built it was
offered `mcp__anc__get_memory`, `get_journal`, `get_dialogs` and
`get_state`: plan section 6.3's scenario, now observed rather than
assumed. That session never called them.

The guard hook was already the layer that caught them. It matches
Anchor's tool names under any server name. `.claude/settings.json`
denied only `mcp__Anchor` and `mcp__claude_ai_Anchor`, so it now also
denies `mcp__anc` and `mcp__claude_ai_anc`, and `tests/test_claude_guard.py`
pins those names. `docs/claude-connector.md` now says to name the
connector `Anchor`: an unknown name is still caught, but only by the
hook's tool-name rule. This also settles the rest of section 11.6: a
cloud session shows a connector as `mcp__<its name>__<tool>`.

## `/delete` voids pending web login codes too

`/delete` truncated `web_session` and closed the web hub, but the web
UI's in-memory `CodeStore` kept any pending login code. A code issued
just before `/delete` could still open a fresh session after it. The
handler now clears it, next to Claude's pending requests
(`tests/test_claude_access.py`).

## Button menus dispatch to the existing handlers

`/menu`'s buttons call the same nested handlers `/checkin`, `/quiet`,
`/focus` and the rest already are, with a built `CommandObject` where
one takes arguments. Every effect, audit row and `_once` replay gate
stays in one place; a button press is its own `update_id`, so the gates
work unchanged. `app/tg/menu.py` only decides whether a button exists
(`action_available`, called both when drawing and when a press
arrives), and the router asserts its dispatch table names the same
actions.

`/export`, `/delete`, `/grok` and `/planner_link` are not in the menu,
and a forged `mn:a:export` answers stale. The web ingress blocks those
commands by name; a menu callback would be a way around that first
layer, leaving only the handlers' own `is_web_sink` guard. `/grok`
opens read access to everything and stays a typed act. Commands that
need typed text (`/due`, `/remember`, `/tz`, ...) are left out too, and
a bare `/due` clears the main action rather than showing it.

## One reply-keyboard button, not a full reply keyboard

A reply-keyboard button sends plain text, indistinguishable from typing,
so each one needs its own handler ahead of the persona turn or it
becomes a chat line. `/start` attaches one persistent `☰ Меню` button
(one such handler, tested for zero model calls); everything past it is
an inline keyboard, whose presses are callbacks. A full keyboard would
also sit under every turn of a chat that is meant to be talked to.

## `/state` is a rich message

Bot API 10.1's rich messages let `/state` be two compact tables (the
core facts, then today's spend and proactive messages) and a collapsed
«Система» block, with a `🔄 Обновить` button that edits the same
message. Timestamps are `date_time` rich text (9.5), so the client
renders them in the reader's locale and the last check-in and the
footer stay relative and current. Each keeps the old string as its
fallback text. The plain view's «Локальное время» line is dropped: a
frozen clock reads stale in a message meant to be refreshed.

`_format_state` and `app/tg/state_view.py` share the helpers that decide
what each line says, and `tests/test_state_view.py` checks every plain
value appears in the rich view. The web chat keeps plain text, since
its sink only understands text messages. A `TelegramBadRequest` on send
or refresh falls back to the plain text, so `/state` is never silent.

## `/menu` is a rich message with in-body buttons

`/menu` gets the same Bot API 10.1 upgrade `/state` did, but with the
buttons themselves inside the message body (`InputRichBlockButtons`/
`RichMessageButton`) rather than a separate `reply_markup`, since 10.1
lets a button block sit anywhere blocks can -- there is no longer a
reason to keep the picture and the controls in two different API
concepts. `app/tg/menu.py` builds one neutral `Section` spec per screen
(a title, an optional hint, an optional status table, and rows of
buttons) and both `render()` (the old plain `(text,
InlineKeyboardMarkup)` pair) and `render_rich()` draw from it, so the
two can never show a different set of buttons for the same settings.
`render()` stays in use for two audiences that never see rich blocks
at all: the web chat (`app/web/sink.py` only understands plain
`SendMessage`/`EditMessageText`) and, for real Telegram clients, the
fallback a `TelegramBadRequest` on send or edit drops back to --
covering both an actual rejection and a pre-upgrade plain menu message
someone taps a button on later. `app/tg/router.py`'s `_show_menu` is
the one place that picks a path and keeps `menu.render`/`render_rich`
from drifting on what each caller passes them.

The new "📚 Хранилище и знания" section moves the existing `vault`
status button out of "Данные и доступ" and adds a status table (mode,
the knowledge/personal flags, notes consent, and -- Telegram only, with
`CLAUDE_ACCESS_ENABLED` -- Claude's library read/write state) plus two
state-dependent toggle pairs: `notes_on`/`notes_off` for
`/vault notes on|off`, and `lib_write_on`/`lib_write_off` for
`/claude library write on|off`. Only the button matching the *current*
state is ever drawn -- never both "on" and "off" for the same switch --
so a tap always reads as "do the thing", not "pick a side that might
already be true". Both pairs are buttons now, not just typed commands,
because both are fully reversible (`/vault notes off` deletes only a
rebuildable index; `/claude undo` can restore whatever the write switch
let Claude change) and `notes_off`/`lib_write_off` get Bot API 9.4's
`danger` style for exactly that reason, `notes_on`/`lib_write_on` get
`success`. The write toggle stays Telegram-only, mirroring `/claude`
itself (`claude_command`'s own `is_web_sink` guard) and gated the same
way in `menu.action_available`, since it opens the same code-approval
surface a stolen web session must never reach. `grok`, `export` and
`delete` are still excluded from the hub entirely -- see `app/tg/
menu.py`'s own docstring on `ACTIONS` for why each one stays a typed,
deliberate act instead of a button two taps deep.

## `/menu` becomes the settings hub

An audit of what the bot stores against what `/menu` could reach found
the controls were there but scattered: a "Режим" section held focus and
pause as *both* on and off buttons with no hint of which was current,
quiet had no way to tell whether it was on, the planner's on/off switch
and Claude's library *read* switch had no button at all, and one real
setting had no writer: `intensity`. Plan §13 lets only commands,
buttons, pause handling or check-in logic move it, but nothing but a
soft pause word ever did -- and that only ever lowers it -- so once
lowered it could not come back up without a SQL edit.

- **`/intensity 1-5`** (`commands_core.set_intensity`, source
  `command`) is that missing writer. It raises rather than clamps on an
  out-of-range value: the command checks its argument first and the
  menu only ever sends 1-5, so a bad value reaching the core is a bug.
  The extractor still cannot reach it (no import, same as before).
- **The hub shows state before it offers actions.** Main and
  "⚙️ Настройки" open with a status table (bot active/paused, quiet
  until, focus, intensity, and the main action on main), read fresh by
  `router._menu_view` on every render. Every switch draws only the
  button that changes the current state, and after a press the router
  re-renders the section it lives in (`menu.REFRESH_SECTION`) -- one
  generic step instead of the vault toggles' own ad-hoc re-render.
- **Intensity buttons carry their target.** "🔽 Мягче → 2" sends
  `int_2`, not "minus one", so a stale or doubled tap lands on the
  number the button showed.
- **Navigation.** Every section ends with "‹ Меню" and "✕ Закрыть",
  and every section is one tap from main and reachable only from there,
  so "back" always means one place. Sections that only lead somewhere else were folded away
  ("Режим" is gone), and "🗓 Планер" joins "📚 Хранилище" as a
  connection section with a status table and the sync switch.
  `/planner_link` stays typed for the same reason `/grok` does.
- **Typed-only commands are named, not hidden.** The settings card says
  how to change the time zone and the main action; the data card names
  `/export`, `/delete` and `/grok`. None of them become buttons -- the
  reasons in `menu.py`'s `ACTIONS` comment still hold.
- **Deploy-level schedule is shown, not editable.** Night quiet hours
  and the morning/evening times are `Settings` values; the settings card
  shows them so the user knows when Anchor speaks. Making them per-user
  would need new columns and changes to the outbound gate, so it is left
  for its own change.

## 8c — `/forget` forgets the whole lineage (§18.1)

Settled: `/forget` and deleting a fact file agree. `memory.forget`
is now a thin wrapper over `memory.forget_lineage`, which resolves any
id forward to the head, then deletes the head and every predecessor.
It writes one `state_change` row (`old_value` = the head's id, no
text). A corrected fact's old text no longer comes back when the
correction is forgotten, whichever path forgot it.
`test_forget_the_head_of_a_chain_clears_the_pointer` still pins
`hard_delete`'s own single-row relink. `forget` no longer calls
`hard_delete`, and the new behaviour is pinned by
`test_forget_the_head_of_a_chain_forgets_the_whole_lineage` and
`tests/test_forget_lineage.py`.

The web panel's «Забыть» dialog used to warn that an earlier version
«снова станет активной». It now says the earlier versions are
forgotten with the record.

## 8c — `FORGET_PROTECTED` covers the whole lineage

"8b on main" kept `main`'s refusal to forget a fact behind an adopted
technique. Before, only the row being deleted was checked, because
`hard_delete` could relink a card to the successor. `forget_lineage`
deletes the whole chain, so no successor is left to relink to. It
refuses if an adopted card points at *any* row in the lineage, and
nothing changes. This also closes a case that used to succeed: an
adopted card on a predecessor was silently moved to the successor
when the predecessor was forgotten. That forget is now refused.
A non-adopted card pointing into the lineage has its pointer cleared,
because `study_card.memory_id` has no `ON DELETE`. `cards.adopt` never
produces such a card; the clear is defensive. No migration: the
`forgotten` card status stays dropped, as "8b on main" decided.

## 8c — the vault's safety limits are constants, not settings

Plan §3 lists `VAULT_DELETE_GRACE_S`, `VAULT_SYNC_WARMUP_S`,
`VAULT_MASS_DELETE_MAX` and `VAULT_HOLD_TTL_DAYS` as environment
variables. Each is a floor or ceiling on forgetting facts or accepting
a rule, and a deploy must not be able to loosen one. They are
constants in `app/vault/limits.py`: 600 s, 300 s, 3 per rolling hour,
7 days, plus the 300-character fact cap. This is the same call as
"8a — limits that are constants, not settings".
`tests/test_vault_limits.py` pins the values and that `Settings` has
no such fields.

## 8c phase B — identity resolution looks up a row by path first, then by lineage

Plan §7.1's table resolves a-e in order, but only b-e need `anchor_id`
at all: "a row exists for this path" is answered by `path`, not by
`anchor_id`, so a corrupt or missing `anchor_id` never stops case (a)
from applying. `app/vault/ingest._resolve_identity` therefore checks
the path first. For b/c/d it walks `anchor_id` forward through
`Memory.superseded_by` to the lineage's current head, then looks for a
*tracked* `vault_file` row on that head. No Memory row at all with
that id (forgotten, i.e. `/forget` or a vault delete already ran) is
case (d), same as a Memory row that exists but has no tracked file yet
(render hasn't caught up with the write cap) — both "ignore anchor_id,
treat as new" the same way, since neither names a file this pass can
claim. An `anchor_id` of the wrong YAML type (a string, a float) is
folded into "no anchor_id" for identity purposes only; the type check
itself still quarantines `bad_type` once a row is chosen, so a
malformed id never corrupts a lineage, it just always resolves as a
new fact that gets quarantined immediately.

## 8c phase B — `technique` is parseable, refused only where the DB says so

Plan §7.1 lists `technique` as a `bad_kind` case in the same breath as
the four vault-writable kinds, but also requires "editing a
technique's text is allowed" — which needs a *parsed* `ParsedFact`
with `kind="technique"` to reach the three-way apply at all.
`ingest.parse_fact` therefore accepts all five `memory.KINDS` values,
including `technique`; the refusal ("the vault can neither create a
technique nor convert to/from one") is enforced in `ingest_file`,
which is the only place that knows whether a row already exists and
what its current `kind` is. A brand-new file (case d/e) with
`kind: technique` is refused there; an existing row's `kind` changing
to or from `technique`, compared against the *head*'s kind (not the
three-way base), is refused there too. Comparing against the head
rather than the base means a stale file that still says `technique`
from before a database-side kind change is not itself an error — only
an actual attempted conversion in this pass is.

## 8c phase B — the pin-cap exception to "quarantined rows are never rewritten"

Plan §7.1 says a `pin_cap` refusal clears `render_digest` "so the next
render puts the property back", and §7.3 separately says quarantined
rows are never written except for exactly this case. The two render
functions (`_render_facts`, `_write_fact`) therefore carry one
deliberate carve-out: a row with `state="quarantined"`,
`reason="pin_cap"` and `render_digest is None` is rendered like an
`ok` row (the file is rewritten to put `pinned: false` back), but its
`state` is left `quarantined` afterward — the rewrite is not a
"fixed" event the way an sha-change re-ingest is. Every other
quarantine reason is inert until the file changes again.

## 8c phase B — a `mass_delete` revert's `restore` state resolves within the same pass

Plan §8's table says a `mass_delete` revert puts rows into `restore`,
"the files come back on the next render" — read literally as the
*next pass*. In this build a hold is decided by a separate call
(`holds.decide`, from the Telegram layer in phase C) between passes,
so in practice the revert and the recreation are already two different
events in time. But the deletions-vs-render ordering inside one pass
(§7's step list has deletions before render) means a `FORGET_PROTECTED`
refusal — which also produces a `restore` row, from inside the same
pass that discovered the refusal — gets its file back before that
pass even returns. `tests/test_vault_deletions.py`'s protected-forget
test asserts the single-pass behaviour rather than assuming a second
pass is needed; nothing stops a second no-op pass either, since
`_render_facts`' restore branch is idempotent.

## 8c phase B — a `pin_cap`/`duplicate_file`/`technique`/`bad_*` quarantined row is never swept as "forgotten"

`_render_facts`' pre-existing "forgotten facts" loop (8b) deleted any
`vault_file` row with `memory_id IS NULL` that was not `held`, on the
assumption that the only way to reach that state was `/forget`'s
`ON DELETE SET NULL`. 8c's ingest creates rows with `memory_id NULL`
for a different reason — a quarantined file that never got a memory of
its own (a duplicate, a new technique, a bad-kind file) — and the old
loop deleted those too, silently discarding the quarantine and the
file's row on the very next pass. The loop's skip condition now also
excludes `state="quarantined"`; a proper "the memory this row pointed
at is gone" cleanup only ever applies to a row that used to be `ok`.

## 8c phase B — `MEMORY_WRITERS` in `tests/test_vault_isolation.py` narrows to the three, not zero

8b's AST check failed closed by forbidding every memory-writing name
anywhere in `app/vault/`, because nothing in 8b's mode dispatch called
any of them. 8c's `ingest.py`, `deletions.py` and `holds.py` now call
`write_memory`, `set_pinned` and `forget_lineage` by name — exactly the
three plan §13 allows — regardless of which mode is configured at
runtime; whether a given call site actually *executes* in `mirror`
mode is a property of `run_vault_sync`'s dispatch, not of what names
appear in the source, so it cannot be an AST check. The rewritten test
keeps failing on the two names 8b forbade outright (`hard_delete`,
`add_pending`) and leaves "mirror applies nothing" to
`tests/test_vault_sync.py`'s own behavioural tests, which is what it
was actually testing all along.

## 8c phase B review — five bugs, fixed with their own tests

A review of phase B found five real bugs, each fixed and each pinned by
a new test proven against a deliberate breaking edit:

1. **A new fact's `pinned: true` bypassed the pin cap.** `ingest_file`'s
   new-fact branch called `write_memory(pinned=parsed.pinned)` directly,
   which has no cap of its own -- only `set_pinned_capped` and 8c's own
   pinned-edit path check `count_pinned`. Fixed: a brand-new file with
   `pinned: true` past the cap is quarantined `pin_cap` and writes no
   memory at all, same refusal shape as an edit that would cross the cap.
2. **A quarantined new file could never recover.** A row created for a
   first-sight quarantine (`bad_type`, `too_long`, `technique`,
   `duplicate_fact`, `pin_cap`...) has `memory_id NULL` by construction --
   there is no memory yet. The existing-row branch read `memory_id NULL`
   as "forgotten" (`/forget`'s `ON DELETE SET NULL`) and skipped it
   forever, so fixing the file on disk never did anything. Fixed:
   `ingest_file` now recognises "quarantined and memory_id NULL" as a
   distinct case and reuses that row as the new fact once validation
   passes, rather than treating every `memory_id NULL` row as forgotten.
3. **The three-way base trusted a hand-edited `anchor_id` from any
   lineage.** If a file's `anchor_id` was edited (by hand, or by copying
   another file's frontmatter) to name a row from an unrelated lineage,
   every field read as "changed" relative to a stranger's text, and the
   merge would supersede the head with content the user never wrote.
   Fixed: the base is only trusted when it actually leads to this file's
   head (`_head_of(base_id).id == head.id`); otherwise the merge falls
   back to file-vs-head, the same degeneration already used when the
   base row is gone outright.
4. **A rule-hold confirm that duplicated another active fact discarded
   the user's yes.** `write_memory` returning `None` (near-duplicate) was
   not checked in `holds._apply_rule`: for an edit, the row went back to
   `ok` with the old text kept silently; for a new file, the row's
   `memory_id` stayed `NULL` and the render pass's normal "memory is
   gone" cleanup deleted the file the user just confirmed. Fixed: both
   paths quarantine the row `duplicate_fact` instead. The hold's own
   `status` stays `confirmed` (the button press was real), but `decide`
   reports a distinct outcome, `DUPLICATE_RESULT`, so a caller (phase C)
   can tell the user why nothing changed.
5. **`VaultHold.created_at` came from the database's `now()`, not the
   `Clock` `expire_holds` compares it against.** A `FrozenClock` set far
   from real wall-clock time (a test, or a resumed pass after a long
   pause) could never open a hold `expire_holds` would ever find due.
   Fixed: `open_rule_hold` and `open_mass_delete_hold` now take `clock`
   and stamp `created_at` from it explicitly, threaded through from
   `ingest.py` and `deletions.py`, both of which already carry a `Clock`.

## 8c phase B review — every test proven by a breaking edit

The review also asked for the breaking-edit proof (make it fail, then
revert) on every test added in this phase, not just the ones from the
first pass -- about 80 across `test_vault_ingest.py`,
`test_vault_holds.py`, `test_vault_deletions.py` and
`test_vault_privacy_logs.py`. All of them were proven; the full table is
in the hand-back report for this round rather than here, since it is a
one-time record of *how* each test was checked, not a design decision
future readers need. Two things worth recording because they are
decisions, not just checklist items:

- **The journal hand-edit test needed a stronger breaking edit than
  disabling the `diverged` state.** Simply skipping the `state =
  "diverged"` assignment left the file protected anyway, because the
  next write still had to pass compare-and-swap against the *stale*
  `disk_sha256` recorded before the hand edit -- so it always got a 412
  and never overwrote anything, accidentally. The breaking edit that
  actually exercises the "never touch it again" guarantee is one that
  also adopts the user's new hash (`row.disk_sha256 = entry.sha256`)
  before falling through to write, which lets the CAS succeed. Recorded
  here so the next person extending `_render_journal` does not "fix" a
  false positive and quietly remove real protection.
- **A crash between `write_memory` and the shared `vault_file` commit**
  has no reachable code path to break inside `ingest.py` itself, because
  the sharing *is* "don't call `session.commit()` in between" -- there
  is no separate line to disable. Its test therefore proves the
  invariant directly against `write_memory(commit=False)`'s own
  contract (insert a memory, add a row, raise before the shared commit,
  confirm both roll back), and the breaking edit is inserting an early
  `commit()` into the *test* to show a real implementation bug would
  have left the memory row behind.

## 8c phase C — notices, holds' Telegram side, `v:`, and `/vault`'s problem list

Everything the plan (§8) leaves to the bot rather than `app/vault/`:
the at-most-one-per-pass notice, sending a pending hold's card, the
`v:y:`/`v:n:` callback, and `/vault`'s "Требуют внимания" list.

- **Where the notice/hold-send logic lives.** `holds.py` already had
  `pending_unsent`/`mark_sent` from phase B (anticipating this), and
  `app/core/report.may_report_now` already lived outside `app.worker`
  (also phase B/8a) -- both moves the plan called for in §8 were
  already done, so phase C only had to *use* them. The formatting
  (notice text, hold card text, the keyboard, the `v:` parse and the
  problem-list labels) all live in `app/tg/vault.py`, one module,
  rather than being split by hold kind -- there is no test pinning
  "app/vault/ must not import app.tg" that would force a split, and one
  place to look is simpler than several.
- **ExtractOutcome gained `vault_pass_result` (a `PassResult | None`)
  rather than following `RESEARCH`'s inline pattern.** `RESEARCH` calls
  its own `_send_research_done` *inside* `_run_job`, before the job is
  marked done, on the session the job itself used. That pattern predates
  the "send after, not inside" rule `process_one_job`'s own comment
  states for `outcome.created`/`order_proposed`/`amendment_trial_id`:
  a send that raises must not roll back or re-run the pass. A vault pass
  can create holds and change counts that must not be re-computed on
  a retry, so `VAULT_SYNC` follows the newer, safer pattern instead of
  copying the older one: the worker only learns the pass finished
  *after* the job is committed done, then hands the `PassResult` to
  `app/tg/vault.send_pass_updates`, which opens its own fresh session.
- **`may_report_now` is asked once, right when the send happens, not
  captured from inside the pass.** The pass and the send can be minutes
  apart in principle (a slow worker, a deferred retry); asking at send
  time is what the plan's "only if `_may_report_now` allows it right
  then" literally says, and it is also what makes "the hold waits" true
  without any extra bookkeeping -- an unsent hold is just a `vault_hold`
  row with `tg_message_id is null`, found again by `pending_unsent` on
  the very next pass.
- **The notice's exact punctuation** (a period before "Не принято" but
  never before the trailing dash, and "Хранилище" alone with no colon
  when every count is zero but a quarantine still happened) came
  straight out of the task's own worked examples; `notice_text` is
  written as one formatting function with no `may_report_now` awareness
  at all, so it stays trivially testable without a database.
- **The `v:` callback is refused from the web chat, at both layers.**
  `app/web/ingress.py`'s `BLOCKED_CALLBACK_PREFIX` gains `v:`, and the
  router's handler checks `is_web_sink` like `d:`/`g:`/`cl:`. Phase C
  first followed the majority of prefixes (`m:`, `nb:`, `c:`), which
  carry no guard. It was changed in review. A `v:` press accepts a
  rule or forgets facts in bulk, and plan §8 exists because a rule
  used to be creatable only from an authenticated Telegram chat. Hold
  messages are never sent to the web chat, so a web press can only be
  forged. Tests: `test_a_press_through_the_web_sink_is_refused_and_changes_nothing`,
  `test_the_web_chat_cannot_press_a_vault_hold_button`.
- **`EARLY_MODE_NOTE` removed, not just untriggered.** `VALID_VAULT_MODES`
  (config validation) and `vault_status.IMPLEMENTED_MODES` have been the
  same four-element tuple since 8b: `settings.VAULT_MODE not in
  IMPLEMENTED_MODES` can never be true for a `Settings` object that
  passed validation, so the branch, its string and its negative test
  assertion were all unreachable dead code once `sync` (the last
  "early" mode) shipped. `vault_status.IMPLEMENTED_MODES` itself is left
  in place -- nothing else names it, but it still documents which modes
  exist, and removing it was not asked for.
- **`vault_problems`'s tie-break is `updated_at desc, id desc`.** The
  task said "then id" without a direction; `id desc` was chosen so that,
  among rows sharing one `updated_at` (a full second's resolution, or a
  batch of writes stamped from the same `clock.now_utc()` inside one
  pass), the most recently *created* row of that group sorts first too
  -- consistent with "most recent first" rather than an arbitrary
  ascending tiebreak.
- **Problem list scope: mirror and sync only, decided at the router,
  not inside `vault_problems`.** `status` mode never runs ingest or
  holds, so its `vault_file` table has no `held`/`quarantined`/
  `diverged` rows to find in practice -- but `/vault`'s handler skips
  the query outright in that mode rather than relying on that always
  being true, so the "status mode: first line only" contract holds even
  if a row somehow existed (e.g. a mode switch after quarantining
  something, without a delete in between).
- **`app.tg` added to `test_vault_isolation.py`'s `FORBIDDEN_IMPORTS`.**
  Not asked for explicitly by the plan text carried into this task, but
  the HARD RULES for this task state "app/vault/ must not import
  app.tg or app.worker" as a peer of the `app.worker` rule the AST guard
  already enforced; leaving the newer half of that sentence unpinned
  while phase C adds a batch of new `app/tg/vault.py` code (the exact
  code app/vault/ must never reach for) seemed like an invitation to
  regress it silently.
- **Every new test proven by a breaking edit** -- the full table is in
  this round's hand-back report, the same convention phase B used
  above.


## 8d — full-text rank does not separate notes from noise; retrieval not built

Plan §9: measure `PERSONAL_MIN_RANK` and `KNOWLEDGE_MIN_RANK` before
fixing them, and stop if true positives and noise do not separate. They
did not, twice. No threshold was set and nothing reads notes into a
prompt.

**Run 1** (`ts_rank_cd(tsv, q, 32)`, 30 notes, 40 messages): true hits
ranked 0.09–0.47, noise up to 0.29. The top hit was right for 22 of 28.
A single shared word («парк», «parc») scored like a real match.

**Run 2** (your choice: a stricter lexical gate, requiring at least N
distinct shared lexemes plus a rank floor). The corpus is in
`scripts/note_rank_corpus.py`: 60 notes, 100 messages (60 true
positives, 40 noise). It was frozen before the first run and no label
was changed afterwards. The pass criterion was fixed in advance:
precision ≥ 0.95, recall ≥ 0.6, and no language below 0.4.

| Gate | Floor | tp | fp | fn | tn | Precision | Recall | ru | fr | en |
|---|---|---|---|---|---|---|---|---|---|---|
| matched ≥ 1 | 0.0909 | 59 | 25 | 1 | 15 | 0.70 | 0.98 | 0.96 | 1.00 | 1.00 |
| matched ≥ 2 | 0.1667 | 49 | 9 | 11 | 31 | 0.84 | 0.82 | 0.68 | 1.00 | 0.85 |
| matched ≥ 3 | 0.1667 | 37 | 5 | 23 | 35 | 0.88 | 0.62 | 0.48 | 0.87 | 0.60 |

No rank floor meets the criterion for any gate. Personal and knowledge
give identical numbers, because the same corpus goes into two tables
with the same `tsvector` formula. Requiring more shared lexemes loses
real matches faster than it removes noise: a one- or two-word overlap
looks the same whether or not the note is relevant.

Kept from 8d: chunking (`app/vault/notes_text.py`), secret masking
(`app/vault/secrets.py`, `redact.secret_spans`), `_chunks.search_ranked`
with no caller in `app/`, and the measurement script. Plan §9 names
this result as the trigger for reconsidering retrieval (pgvector), not
for lowering a threshold. Any embedding model must run locally: 8e §10
forbids personal note text from leaving the system.

## W1 — Claude writes knowledge notes: plan written, decisions pending

`anchor-claude-write-plan.md` specifies how Claude, through the Anchor
connector, would update, create and link knowledge notes with undo in
place of a per-write approval. Nothing is built. The ten decisions in
the plan's §13 are open. Two of them change the brief's wording:
- a secret in written text refuses the write rather than being masked;
- the undo store sits on the vault volume outside the vault root,
  because Railway allows one volume per service.

W2 also waits on C3 and on knowledge indexing, neither of which exists
yet (see "8d — full-text rank does not separate…").

## Index knowledge notes only

Your decision, before C3. The sync pass (step 8) indexes notes whose
class is `knowledge`, only while `notes_consent` is on and
`VAULT_KNOWLEDGE_ENABLED` is set. Personal notes get no row and no
chunks: nothing reads them, because retrieval failed its gate ("8d —
full-text rank does not separate…"), so indexing them would keep a
derived copy with no use. `app/vault/sync.py` does not import
`notes_personal`, and a test pins that.

- **Modes:** `mirror` and `sync`. Indexing writes nothing to the vault,
  so the record-vs-apply split that matters for facts does not apply.
- **Leaving knowledge:** a note that disappears, becomes unclassified or
  `never`, or turns personal loses its chunks and then its row. Nothing
  is reclassified in place, so the composite FK's ordering never comes
  up.
- **Flag off:** with consent on and `VAULT_KNOWLEDGE_ENABLED` off, every
  pass removes the existing knowledge index, so turning the flag off
  leaves nothing stale. Consent off was already handled by
  `/vault notes off`.
- **Pacing:** at most `NOTES_MAX_PER_PASS` (50) note fetches per pass, a
  constant, so a large vault bootstraps over several passes. Each note
  is its own transaction. A note that 404s mid-pass, or fails to
  decode, is skipped without failing the pass.
- **Title:** the chunk heading's title is the file name without `.md`,
  never taken from frontmatter.

## C3 — `search_library` without the failed threshold

Your decision: `search_library` returns the **top 6** knowledge chunks
by `ts_rank_cd`, keeping only chunks that share **at least 2 distinct
lexemes** with the query. On the frozen corpus that gate measured 0.84
precision and 0.82 recall.

The 8d threshold failed for *automatic* retrieval into the persona's
prompt, where an irrelevant chunk steers the reply. `search_library`
is an explicit search that Claude chooses to run, and Claude judges
the results. What an irrelevant chunk costs there is knowledge text
sent to Anthropic that the question did not need. The 2-lexeme floor
cuts most single-word matches without claiming to separate relevance.
Automatic persona retrieval stays unbuilt.

## C3 — the library switch, and what it does not do

- **A standing switch, not a window.** `oauth_connection.library_read`
  (default false) gates `search_library`. It is not a scope in a
  window. `notes_knowledge` joins `ck_access_grant_scopes` as the
  connector plan asks, but no code puts it into a grant today: the
  CHECK is widened so it can never be `notes_personal`, and a migration
  test pins that.
- **What turns it off.** A new connection starts off. `/claude
  disconnect`, a replaced connection and a turned-off
  `CLAUDE_ACCESS_ENABLED` all clear it with the connection. `/revoke`
  clears it but keeps the connection. `/delete` truncates both.
- **What counts as a read.** Only a successful search. A refusal (switch
  off, notes off) is not counted. The count is stored per local date in
  `claude_library_read(local_date, count)`, with no query and no text.
  It is omitted from `/export` as a derived counter and truncated by
  `/delete`.
- **The digest** is one job a day at 21:00 local (a constant), with
  `dedup_key` per local date. It sends nothing on a day with zero
  searches. Inside a quiet period (`may_report_now`) it defers by 15
  minutes and sends on the first allowed tick.
- **The result shape** is `«heading»: text`, the same as the other
  notes readers. It never carries a chunk id or a path.
- **The two-lexeme floor is applied in SQL**, before the limit, so a
  pile of higher-ranked one-word matches cannot crowd out a real match
  (review fix).

## W1 — decisions settled

The write plan's §13 is settled (`anchor-claude-write-plan.md` rev. 2):
- no delete; rename and move are allowed within knowledge folders, with backlinks rewritten and refused if any non-knowledge note links there;
- its own write switch;
- wikilinks only;
- a changeset is a 10-minute idle window;
- undo is kept 14 days;
- a secret in written text refuses the write;
- the write routes use the same `VAULT_API_TOKEN`;
- Claude's nodes will be labelled once retrieval exists;
- the undo store is on the vault volume, outside the vault root.

One addition follows from the rename: while the write switch is on,
`search_library` results carry the note's path and hash, and
`get_note(path)` returns a whole knowledge note, so Claude can name the
note it edits. With writing off, no path reaches Claude, as in C3.

## W2a — vaultd's knowledge writes, rename and undo

- **One lock, one code path.** Knowledge writes, renames and undo
  restores go through `Store.put_unchecked`/`delete_unchecked`, the same
  compare-and-swap and atomic-write code as Anchor's own files, all
  under the store's single lock.
- **Checks come first.** Caps are checked by a pure `precheck` before
  anything is written, so a cap refusal never needs a rollback.
- **The rename scans the whole vault**, `Anchor/` included (read
  in-process, never returned). A rename is refused if any
  non-knowledge file links to the note, or if the basename is
  ambiguous anywhere.
  - Links are matched by basename: `[[x]]`, `[[x|label]]`,
    `[[x#heading]]`, `![[x]]` and `[[folder/x]]`. A rewrite keeps the
    label and heading and drops the folder prefix.
- **A rename is all or nothing.** Everything is planned before the
  first write. If a backlink file changes mid-rename (an `ob` race),
  what was already written is rolled back and the answer is 412. If
  deleting the old path fails after the new one was created, the
  changeset is still recorded, so undo removes the duplicate (500).
- **Undo** runs a changeset's entries in reverse, so a file written
  twice in one changeset unwinds correctly. An undo changeset records
  no pre-images, because undoing an undo is refused.
- **Where undo lives.** The undo root is `VAULT_UNDO_ROOT` (default
  `/data/anchor-undo`), and vaultd refuses to start if it resolves
  inside the vault.
- **Every acceptance refusal is the same bare 403.** CAS stays 412,
  and a missing file stays the existing 404.
- One destination-class check in rename was removed as provably
  redundant with the folder-rule check, with a comment saying why.

## W2b — the bot's write path

- **Write needs read.** `oauth_connection.library_write` is set by
  `/claude library write on`, needs `library_read`, and is cleared
  whenever read is, and by `/revoke`, `/claude disconnect`, a replaced
  connection and flag-off.
- **Paths reach Claude only while writing is on.** `get_note`,
  `list_changes`, and path plus hash in `search_library` results.
  `undo_changeset` works with writing off, because it only restores
  your text.
- **Changesets** are minted by the bot, one per connection per
  10-minute idle window. `claude_changeset` holds ids, counts and times
  only: files, bytes, refused, created, renamed. `/export` carries it
  and `/delete` truncates it.
- **Caps are constants and ledger-backed.** Every cap, creates per day
  included, is counted from `claude_changeset`, so a restart cannot
  loosen one. A refused write still opens (or reuses) a changeset, so
  refusals count toward the hourly batch cap; that is the stricter
  direction.
- **One refusal text**, «Запись отклонена.». The reason code goes to the
  log only.
- **The digest's write line** takes titles from vaultd's
  `/v1/changes` at send time (Telegram only, never stored or logged).
  The «создана»/«переименована» markers come from the counters and the
  file order of a rename; in a batch that mixes a rename with other
  writes they are best-effort.
- **[Откатить всё за сутки]** uses `cu:<date>:<epoch>`, is blocked from
  the web chat at ingress and in the router, and answers «Устарело» to
  a stale or repeated press.

## vaultd logs why it refused a knowledge write

Production showed `PUT /v1/knowledge` → 403 twice, with no way to tell
which rule refused it. Each `Refused` / `CapExceeded` now carries a
required reason code from a closed set of 25 (e.g. `folder_not_knowledge`,
`folder_missing`, `name_taken`, `settings_invalid`, `cap_files`). vaultd
logs it once as `event=knowledge_refused, route, reason`. The response
is unchanged, a bare 403 or the same 404, so Claude still cannot tell
the refusals apart. `reason` joins vaultd's safe log keys; its only
values are the closed-set codes, never a path or a name.

## Rev. 3 — Claude places notes itself, inside knowledge roots

Production showed `create_note(folder="Philosophy")` refused as
`folder_missing`: Claude could not see the structure, and vaultd
created no folders. Your decisions:

- **Claude decides placement**, from `list_tree()`: knowledge folders
  and knowledge note titles, no text, only while writing is on. A
  personal-marked note inside a knowledge folder never appears, and
  neither does anything under `Anchor/`. The bot and vaultd still make
  no model call on the write path.
- **vaultd creates missing folders**, but only below a folder already
  covered by `knowledge_folders`, and only where the whole new path
  resolves to knowledge (a `never` or `personal` rule refuses it).
  - Limits: at most 4 levels below that root, 3 new folders per
    changeset, 10 per day.
  - Segment names are checked: NFC, no leading dot, no separators or
    control characters, 120 characters at most.
  - It never creates a top-level folder, and `Anchor/settings.md`
    stays unwritable, so the roots stay yours.
  - Undo removes a created folder only if it is empty afterwards,
    deepest first and never recursively.
- **Moves have their own budget**: 20 files per changeset (a moved
  file plus its rewritten backlinks) and 60 a day. Content writes keep
  5 per changeset, and they are counted separately even within the
  same changeset. Changesets per hour stay 4.
- `folder_missing` still means "no knowledge-covered ancestor". New
  codes: `folder_not_under_knowledge`, `folder_too_deep`,
  `folder_name_bad`, `cap_folders`, `cap_folders_day`, `cap_moves`,
  `cap_moves_day`.
- The digest says «Создал N папку/папки/папок.» when folders were
  created. `claude_changeset` gains `folders` and `moves` counters, and
  still holds no text or paths.

## Claude's content-write caps raised: 20 files per changeset, 40 new notes a day

Production: a batch of one topic note plus its people and concept
notes stopped at the sixth file (`cap_files`). Five files per
changeset and ten new notes a day were too tight for building out a
topic in one go. Your decision:

- `FILES_PER_CHANGESET` 5 → 20, in both copies (app/core/
  claude_write_limits.py and vaultd/vaultd/config.py), kept equal.
- `CREATES_PER_DAY` 10 → 40 (bot-side only, as before).
- Unchanged: 4 changesets an hour, 4 undos an hour, 64 KB per file,
  512 KB per connection per day, the folder and move budgets.
- They stay code constants, never environment variables.

## Claude write caps become settings (Telegram and the web app)

Raising the caps by hand meant a code change and a deploy every time
(the section above). Your decision: the rate/volume caps become
settings you tune yourself, from Telegram and from the web app. This
supersedes "they stay code constants" above, and "8c" / "constants,
not settings", **for these nine caps only**:

| key | default | bounds |
|---|---|---|
| `files_per_changeset` | 20 | 1–200 |
| `changesets_per_hour` | 4 | 0–60 |
| `creates_per_day` (bot only) | 40 | 0–500 |
| `bytes_per_day` (bot only) | 512 KB | 0–8 MB |
| `undos_per_hour` | 4 | 0–60 |
| `folders_per_changeset` | 3 | 0–20 |
| `folders_per_day` | 10 | 0–100 |
| `move_files_per_changeset` | 20 | 1–200 |
| `moves_per_day` | 60 | 0–600 |

- **Defaults are the old constants**, so nothing changes until you edit
  a value. 0 means none allowed.
- **Telegram:** `/claude limits` lists them; `/claude limits KEY N` sets
  one; `/claude limits KEY reset` and `/claude limits reset` restore
  defaults. `bytes_per_day` also takes `512k` / `2m`.
- **Web app:** a «Лимиты записи Claude» card on the state screen
  (`POST /api/state/claude-limits`). The web may **raise as well as
  lower** a cap. That is a deliberate exception to "Claude settings are
  Telegram-only": a stolen web session can now widen how much Claude
  may write per hour/day, but not turn writing on, open a window, or
  connect Claude -- those stay Telegram-only.
- **Storage:** the bot is the source of truth (`claude_write_limit`, one
  row per override, no content). vaultd keeps its own copy of its seven
  caps in `<undo_root>/limits.json`, outside the vault, set by
  `PUT /v1/limits` with the same bearer token. vaultd holds each value
  to the same bounds, so no push can set a nonsense number. The bot
  pushes on every change; a failed push sets
  `vault_status.limits_push_pending` and the vault sync pass retries
  it. While the copies disagree, the stricter one wins.
- **Still constants:** 64 KB per note (it touches vaultd's body and read
  caps), folder depth 4, the 14-day undo TTL and the 10-minute
  changeset window. Nothing comes from the environment: a deploy still
  cannot widen a cap by pasting a variable.
- `/delete` wipes the overrides, and vaultd's `POST /v1/purge` resets
  its copy. `/export` includes the overrides.

### Follow-up: keyboard, self-healing sync, review fixes

- **Telegram keyboard.** `/claude limits` carries a ➖/➕ row per cap
  (`cw:s:<key>:<value>`, the value the press sets, never a step, so a
  doubled press is harmless) and «↺ Все по умолчанию» (`cw:r`). The
  menu's vault section has «Claude: лимиты записи». Telegram only:
  `cw:` joins `BLOCKED_CALLBACK_PREFIX`.
- **Sync heals itself.** The pending flag alone could lie: a slow push
  of old values landing after a newer one, a failed push in `status`
  mode (no sync pass runs there), vaultd losing `limits.json`, or a
  `/v1/purge` resetting it after a cap was set. Now every vault sync
  pass calls `reconcile`: `GET /v1/limits`, push only when it differs.
  The pending flag is cleared only when what vaultd holds matches what
  the bot wants *after* the request. `/state`'s and `/vault`'s probe
  retries while a change is pending, which covers `status` mode.
- **`bytes_per_day` is whole KB.** It is shown and edited in KB
  everywhere, so a byte count between two KB is refused; a plain
  number in `/claude limits bytes_per_day N` means KB.
- **Undo store.** Raising the move caps to their maxima (600 moved
  files a day) lets `<undo_root>` hold up to about 1.7 GB of
  pre-images over the 14-day TTL, against about 170 MB at the defaults.
  Accepted: it only happens if you raise them.

## Claude write counters can be reset

Hitting a daily cap (say, 40 new notes) meant waiting until midnight
or raising the cap. Your decision: a «сбросить счётчики» that starts
every hourly and daily count over, without touching the caps.

- **Telegram:** `/claude limits counters`, or «🔄 Обнулить счётчики»
  (`cw:c`) under `/claude limits`. **Web app:** «Обнулить счётчики» in
  the «Лимиты записи Claude» card (`POST
  /api/state/claude-counters/reset`), which also shows when they were
  last reset. Like the caps, the web may do this: a stolen web session
  could keep handing Claude fresh budgets (each reset is one panel
  write, rate-limited like the rest), but could already raise the caps
  themselves, and still cannot turn writing on.
- **How:** a moment, not a deletion. `vault_status.claude_counters_reset_at`
  is stamped (whole seconds); every hourly/daily count -- changesets,
  undos, creates, bytes, folders, moves -- only counts changesets
  started at or after it. The next write opens a fresh changeset, so
  the per-changeset caps start over too. The ledger is untouched, so
  undo and the digest still see everything.
- **vaultd** gets the same moment as `counters_reset_at` (unix
  seconds) in `PUT /v1/limits`, through the same push and self-healing
  reconcile as the caps, and applies it to its own hourly and rolling
  24h counters. The bot sends the key only once a reset has happened,
  so a vaultd that predates it keeps working until then (a reset
  pushed to it is refused and stays pending; the bot's own counters
  are reset either way).
- `/delete` leaves the moment in place: it is a timestamp, no content.

## L1 — the lens

`anchor-lens-plan.md` rev. 3, milestone L1: the `lens` class, the
graph, the bot's copy of the lens, and Claude Code's read-only door to
it. Echo itself does not use the lens yet (L2). Your decisions (§14):
the lens is material Echo studies, never your views; under 50 notes
today; people and concepts by folder (`lens_person_folders`).

- **Claude Code reads through a role and two functions, not a view.**
  `anchor_lens` has `EXECUTE` on `lens.notes()` and `lens.graph()` and
  nothing else. A view cannot write, so a `SELECT` on one leaves no
  trace; a `SECURITY DEFINER` function inserts its `lens_read` row
  before it returns. That count feeds `/lens` and the digest line,
  «Claude Code прочитал линзу: N раз». The row is in the caller's
  transaction, so a rollback removes it; each call therefore takes the
  row's id first, from the table's sequence, which no rollback undoes,
  and `/lens` (and the digest) report the gaps as reads without a
  record. Not an autonomous write (no `dblink` extension to depend on),
  but no read goes unseen. The functions pin `search_path` and are
  revoked from `PUBLIC`. The door is `LOGIN`, which you flip with
  `/lens code on|off`; the password is set once by hand, as for
  `anchor_debug`, so the bot never holds it.
- **Lens is below knowledge in strictness** (`never > personal >
  knowledge > lens`). Lens is a kind of knowledge, so every existing
  consumer (the index, `search_library`) keeps working unchanged, and
  "stricter wins" can only ever make the lens *smaller*:
  - `anchor: knowledge` on one note in a lens folder excludes that note;
  - `anchor: lens` inside a personal or never folder is not lens;

  Two shapes make the settings file invalid (fail closed), like any
  other settings error, because each is a mistake you would never see:
  - a lens folder nested in a knowledge, personal or never folder.
    Stricter wins would turn all of it into the outer class: a lens
    that is silently empty, and under a knowledge folder writable by
    Claude. (The first draft let it resolve to knowledge; review found
    that the plan's own example, `Library/Lens` under `Library`, hit
    exactly this.)
  - a `lens_person_folders` entry outside every lens folder: a person
    rule that covers nothing.
- **W2 cannot write lens notes.** `PUT /v1/knowledge` and
  `POST /v1/knowledge/rename` refuse a lens note as the source, and a
  write or move whose destination would be lens, with the existing
  "not knowledge" refusal. The lens is what Echo will reason with when
  it changes itself. A claude.ai chat, or text it read that carries
  instructions, must not be able to reshape it; a move into a lens
  folder would be a model choosing lens membership, which is yours
  alone. Only you change the lens. `list_tree` marks lens notes so
  Claude knows before it tries.
- **The graph never names an outside note.** A link from a knowledge
  or lens note to a note that exists but is personal, never,
  unclassified or under `Anchor/` becomes `{"outside": true}`: no path,
  no title, not even the link's own target text, because `[[Name]]`
  *is* the name. It is counted so the graph's shape stays honest.
  Existence and name of a hidden note are its content, as 8e already
  holds for `/vault` ("invisible notes are counts, never paths"). A link
  to no note at all keeps its target text (`unresolved`): that text is
  written in a note the bot may already read. `frontmatter`'s
  `aliases`, `tags` and `summary` are read for knowledge and lens notes
  only.
- **Storage.** `lens_note` holds each lens note whole, only while notes
  consent, `VAULT_KNOWLEDGE_ENABLED` and `LENS_ENABLED` are all on; any
  one off deletes the rows on the next pass, as the knowledge index
  does. `note_link` (knowledge and lens links, from the graph) follows
  the knowledge gates. `lens_version` gets a row only when the hash over
  the sorted body hashes and lens-to-lens edges is new. Only
  `app/vault/lens.py` touches the four tables, pinned by
  `tests/test_vault_notes_isolation.py`. Their debug views carry ids,
  hashes, lengths, booleans and counts; `debug.note_link` has
  `unresolved` as a boolean, never the text.
- **The digest runs with `LENS_ENABLED` alone.** Lens reads need no
  connector, so the daily Claude digest is queued when either
  `CLAUDE_ACCESS_ENABLED` or `LENS_ENABLED` is on, and still sends
  nothing on a day with nothing to report.
- **`/lens` is Telegram-only**, like `/claude`: opening a database door
  is an access decision a web session must not make.
- **Lens text stays out of the repo.** CLAUDE.md lets Claude Code read
  lens notes and forbids copying their text into commits, PRs, code,
  fixtures, eval cases, logs or artifacts: it paraphrases from public
  knowledge and cites the note by id. `lens_read` shows how often it
  reads.

## L2 — the review reads the lens

`anchor-lens-plan.md` rev. 3, milestone L2: the weekly review picks
lens notes for itself (§7), grounds its proposals in them (§6's
block), and the card says which notes it leaned on. The lens is
active for a round only while `LENS_ENABLED` is on and the lens holds
between 1 and `LENS_CATALOG_MAX_NOTES` notes; otherwise the review
makes the same single call with the same prompt and input as before,
byte for byte, and records no round.

- **Two extra single-shot calls, not a tool loop.** A loop would give
  one context the week, the lens and the means to fetch more of either.
  Instead pass 1 (the existing analysis, unchanged) runs first, then a
  selector call picks notes, then a grounding call rewrites the
  proposals. Each call has one input, one strict JSON schema and
  temperature 0, on the model the review already uses; each output is
  validated before the next call sees it. Every step can be audited
  on its own, and none can act.
- **The selector never sees the week.** Its input is pass 1's
  validated analysis (wins, misses, patterns, intentions, proposals as
  JSON) and the catalog: id, person or concept, title, summary (or the
  first 300 characters of the body), linked lens titles, and
  `rounds_since_used`. The grounding call gets the same analysis and
  the selected bodies. Neither call gets the raw week input, so
  `load_week`'s welfare exclusion needs no second copy, and a lens note
  cannot pull a dialog line into a prompt it was never in.
- **Grounded proposals replace pass 1's, not add to them.** The
  grounding call returns the whole proposal list under the same kinds,
  caps and screening as today, each with `grounds` (note titles, kept
  only if selected). Adding would double the cards for one week and
  invite the review to argue with itself; replacing keeps one set,
  each proposal resting on the lens where it helps and standing alone
  where it does not. The lens is framed as material the user studies,
  never their views, and the review's own prohibitions (no raising
  intensity, no punishments) outrank any note: a note arguing for
  acceleration cannot become a proposal to push harder. The eval pins
  both.
- **The lens never fails the review.** A provider error, invalid JSON,
  a schema miss or the spend cap on either extra call keeps pass 1's
  proposals, and the round is recorded as `fallback` (with an empty
  selection if the selector itself failed). An empty selection is a
  real answer, recorded as `empty`: some weeks no idea fits. Both calls
  are ledgered and capped exactly like the analysis call.
- **`rounds_since_used` keeps the lens from collapsing onto two
  favourites.** Each catalog line says how many review rounds have
  passed since the note was last selected («никогда» if never), and
  the prompt asks for at least one note unused for four or more rounds
  when one is relevant. The count comes from `lens_round`, so it
  needs no extra state.
- **`lens_round` records every active round**: the lens version, the
  selected ids, the selector's `why`, the outcome. `review_proposal`
  carries `lens_round_id` and `lens_note_ids`, so the card can show
  «основание: …» with the notes' current titles and answer «почему эти
  заметки?» with the rationale. `debug.lens_round` has everything but
  the rationale, and the new `debug.review_proposal` has ids, kind,
  status, times, `lens_round_id`, `lens_note_ids` and the text's
  length, never its text or reason; `lens.rounds(n)` gives Claude Code
  the last rounds with their outcome and the picked notes' titles
  through the `anchor_lens` role, logged in `lens_read` like the other
  two functions. **The rationale is derived from the week**: the
  selector writes it from the first pass's analysis of the user's
  conversations, so it stays with the user -- Telegram only -- and
  neither `lens.rounds(n)` nor `debug.lens_round` carry it (CLAUDE.md's
  first rule: Claude Code never reads conversation data, and model text
  written from it counts). The selector's prompt still keeps the week's
  facts out of it, speaking of the notes and of what Echo should change,
  as hygiene for what the user reads. The *selection* itself (which ids,
  in what order, and an `empty` outcome) stays readable, in
  `lens.rounds(n)` and as ids in `debug.lens_round` and
  `debug.review_proposal`: it says which of the user's own lens notes
  the model reached for, not anything about the week, and it is exactly
  what Claude Code needs to see how the lens is used. `/delete` erases
  `lens_round` and `/export` leaves it out, as for the other lens
  tables. Only
  `app/vault/lens.py` touches it; `app/core/lens_review.py` holds the
  selector and grounding logic.
- **Lens text now reaches the model provider**, during the weekly
  review and only while `LENS_ENABLED` is on: the catalog's summaries
  and the selected bodies go through OpenRouter like the review's own
  input. `docs/privacy.md` says so.

## L3 — the lens garden

`anchor-lens-plan.md` rev. 3, milestone L3 (§8): once a week Echo
reads the lens as a graph and proposes gaps (a missing link, a missing
note, a tension, a bridge between clusters), in Telegram and as a note
in the vault. Echo never edits a note here. The L3 spec settled the
details; you amended two of them (one message per run, and
`lens.gaps` showing `closed`).

- **An idle kind, `lens_garden`, not a new job.** The idle gate,
  budget, preemption and `/digest` line come with it. `KIND_DAILY_MAX`
  is 1, and the job's own gate adds what a daily cap cannot: the flag
  (`LENS_GARDEN_ENABLED`, off by default, on top of `LENS_ENABLED`,
  the knowledge index and a sync `VAULT_MODE`), 3 to
  `LENS_CATALOG_MAX_NOTES` notes, 168 hours and a new local ISO week
  since the last run (`iso_week` is unique, which also covers DST), and
  a lens that changed since that run or a gap marked done. Without the
  last rule a static lens would get marginal gaps every week. A failed
  run (provider error, bad JSON, the spend cap) writes nothing and
  retries tomorrow; there is no templated fallback, because the model
  is the filter on step 1's recall. **This amends phase 6 §8**: idle
  may now write two tables, `lens_garden_run` and `lens_gap`, through
  `app.vault.lens` only (`tests/test_idle_isolation.py` allows that one
  module to that one file, and still bans `app.tg`).
- **Step 1 is in-house, stdlib only; no networkx.** Orphans, dead ends,
  wanted notes, unlinked mentions, Brandes betweenness, label
  propagation, TF-IDF holes, people without concepts and staleness
  come to about 80 lines over a graph of at most a few hundred nodes,
  deterministic by construction (ascending ids, ties to the smallest
  label). A dependency the bot would carry for one weekly job, whose
  community detection is randomised unless seeded, bought nothing.
  **Known weakness, kept as specced:** label propagation in place, in
  ascending id order, with ties to the smallest label, lets the lowest
  label flood a connected component. Two cliques joined by one edge are
  one cluster (unless their ids interleave), so in practice clusters
  are mostly the connected components, they depend on how sync numbered
  the rows, and holes and bridges appear only between components.
  `tests/test_lens_graph.py` pins the two-clique case, so replacing it
  (a greedy seeding pass, or a deterministic modularity pass) is a
  deliberate change. A hole is "not joined" by exactly the bridge
  recheck's test (a path of length 2 or less in `G_all`, through any
  note), so the model is never pointed at a hole the code would refuse.
- **Step 2 sees the lens only.** One call on `LLM_MODEL_SAFETY` with its
  own provider and `GARDEN_MAX_TOKENS` (4000; 2000 until the first
  real run, on ~90 notes, was cut off): the shared safety
  provider's 400-token cap would truncate ten gaps in Russian. Its
  input is the findings, and for the involved notes their title, kind,
  catalog summary (the frontmatter summary, else the start of the text,
  as in the L2 catalog), lens links and a count of knowledge
  neighbours; never a whole body, a knowledge note's title, a dialog or
  memory, nor the count of links to notes the bot may not see (step 1
  uses it in code only, and `lens.graph()` never shows it). A knowledge note appears in the graph as
  an anonymous id, and its title is only matched locally (a proposed
  missing note that already exists is dropped). This narrows §10's
  "titles, for context", and it is what makes `lens.gaps(n)` safe
  below.
- **Dedup by a title signature, not by ids.** A gap's key is
  `sha256("v1|kind|" + sorted normalised titles)`: the note pair, the
  proposed title, or the bridge's anchors. `_index_notes` keys rows by
  path, so a moved note gets new ids, and an id key would re-raise
  every gap you dismissed. A partial unique index (status other than
  `resolved`) makes a live signature impossible to raise twice; only a
  resolved one may recur. The cost: renaming a note resolves its gaps
  (the recheck no longer finds the title), which may then return under
  the new name, and two notes with one basename collide. Every run
  rechecks open and done gaps by title: one that now holds, or whose
  note is gone, is resolved; one you marked done that still fails is
  reopened, moved to the new run and shown again with «снова».
- **Delivery rides the vault pass, not the idle job.** The idle job
  sends nothing (idle never reaches Telegram). A sibling of the vault
  pass's own update hook sends the run once the pass has written the
  report, so the header can name the note, and retries an unsent run
  every minute. It waits for quiet hours, `/quiet`, a pause and the
  welfare cooldown, like the weekly review, and it is an
  out-of-character report: no `Outbound` row, no counters.
- **One message per run** (your amendment; the spec had one card per
  gap, up to ~15 at once). The header has the week, the counts of new,
  reopened and older open gaps, and the report's path; then the gaps,
  numbered. The keyboard has a row per open gap, «N · закрыл» and «N ·
  не нужно» (`lg:d:<id>:<epoch>` and `lg:n:<id>:<epoch>`; the epoch
  makes a pre-`/delete` button stale). A tap updates that gap and
  edits the same message: the item gains «— отмечено: …», its row
  goes, and the keyboard goes with the last row. Each gap stores the
  message id, and the run stores the gaps it was sent with, so the
  numbering holds when the message is re-rendered, with each gap's
  «снова» count as sent (`sent_reopened`), so a later run reopening one
  of them never rewrites the old message's counts. A run still unsent
  when the next is recorded (a /quiet renewed for a week, a pause,
  failed sends) hands its open gaps to the new run, which carries them
  in its message, and is marked sent with nothing sent: otherwise those
  gaps would never get a row while their signatures blocked them from
  being raised again. The header names the report only once vaultd has
  confirmed the create (`disk_sha256` set). Telegram's 4096
  characters may shorten details; the report has them whole.
  «Исследовать» waits for L4: a dead button is worse than none, and a
  tap must not become paid research under pre-L4 terms (`lg:r:` is
  reserved, and stale).
- **The report is a third writable folder, `Anchor/Reports/`.** vaultd's
  writable set and purge gain it (deploy the vault service first; the
  bot catches an old vaultd's `REFUSED`, counts it as
  `reports_refused`, and never lets it roll back the pass). The path
  is `Lens garden <week>-<epoch>.md`: with no epoch, a pre-`/delete`
  copy re-uploaded by an offline device would take the name and leave
  the row `NAME_TAKEN` for good. It is written like a journal day:
  create-only, then compare-and-swap on the digest; a hand edit makes
  it `diverged`, a deletion (sync) `dismissed`, and neither is written
  again. Only the latest run with gaps is rendered, after the notes
  index so it cannot starve it. The note says edits there are not read
  -- the buttons are the interface -- and `FACT_PATH_RE` never ingests
  it. It links only to lens notes (W2 cannot rename those), leaves
  proposed and wanted titles as plain text, escapes everything the
  model wrote (no `[[`, `]]`, `|`, `#`, link, HTML or line break), and
  stays under 60 KiB by dropping «Структура» first.
- **`lens.gaps(n)` for Claude Code, with `resolved` shown as `closed`**
  (your amendment). The function returns the last gaps with their
  week, kind, titles, proposed title, detail, reopened count and
  times, logged in `lens_read` like the other three. Its text is safe
  where `lens_round.rationale` is not: the model that wrote it saw lens
  notes only (above), which `lens.notes()` and `lens.graph()` already
  expose, and a test pins that input. The status is `open`, `done`,
  `dismissed` or `closed`, and there is no `resolved_at`: a resolved
  missing note would otherwise tell Claude Code that a note with that
  title now exists, perhaps a knowledge note it may not see. L4's
  `researched` is `closed` too. `debug.lens_gap` maps the status the
  same way and carries no text, recheck payload or signature (a hash of
  a few short titles can be guessed); one inference remains, a closed
  gap whose notes still exist was resolved by the recheck.
  The word alone was not enough for a missing note: its sources still
  in the lens and no lens note by that title, `closed` would still mean
  "a knowledge note (or a lens alias) by that name exists". So a closed
  `missing_note` comes back with no title and no detail. **Chosen: the
  minimal fix.** An open missing note's proposed title is still shown,
  so Claude Code can join an earlier read to a later "closed" by id;
  the stronger fix (never returning a missing note's proposed title)
  stays available if that correlation matters. And like `lens.rounds`,
  `lens.gaps` forgets a note that left the lens: `titles` are current
  titles (a departed note skipped), and a gap any of whose notes has
  left comes back without title and detail, which may name it. A note
  moved within the lens gets a new id, so its gaps lose their text
  too: the cautious side.
- **Aliases are stored now** (`lens_note.aliases`, for mention matching
  and missing-note checks): taken from the graph, dropped when the
  notes mask would change them, kept from the last pass when the graph
  is missing or truncated, and left out of the version hash. Tags are
  not stored.
- **The garden dies with the lens.** `lens.delete_garden` runs wherever
  `lens_note` is emptied: the knowledge index or the lens turned off
  (next pass), and notes consent off, in the same transaction as the
  consent change, because that path deletes the notes by cascade and
  the garden has no key to a file. `/delete` truncates both tables and
  purges `Reports/`; `/export` leaves them out like the other lens
  tables. Logs carry ids and counts, never a title, alias, term,
  detail, path or signature.
- **Lens text reaches the model weekly while the garden is on**:
  titles, catalog summaries (or the start of the text), lens links and
  counts of links to knowledge notes, through OpenRouter.
  `/privacy` and `docs/privacy.md` say so, and that Claude Code can read
  the proposals.

## L4 — lens research into `Echo/Inbox`

`anchor-lens-plan.md` rev. 3, milestone L4 (§9, §13, §14.4–14.5): a gap
the garden raised can be researched on the web, on your tap only, and
what you accept becomes one knowledge note in the vault's inbox, never
a lens note. The L4 spec settled the details; you amended two of them.

- **Amendment (a): `PACKET_LENS` leaves out `archive.org`.** Its
  default is `plato.stanford.edu`, `iep.utm.edu`, `philpapers.org`,
  `arxiv.org`, `en.wikipedia.org`, `pangaro.com` and
  `asc-cybernetics.org`, parsed like the other packets, at most 12
  (more raises at startup rather than trimming). The plan listed
  `archive.org`, but the packet check accepts subdomains, so it would
  let in `web.archive.org`, which serves a copy of any site: the
  allowlist would allow everything. It is a separate packet, so
  `/study lens` stays refused and philosophy sources never widen
  `/study`'s.
- **Amendment (b): the result is its own message, sent as soon as the
  job finishes.** The spec (and plan §9) had results wait for the next
  garden message, with an «Исследовано» section, delivery-only garden
  runs and research moving between unsent runs. That is up to nine days
  of latency for a tap, and a lot of machinery to get it. Instead, the
  worker hook that sends the garden message (after every vault pass)
  has a sibling with its own try/except that sends one message per
  finished lens job, under the same holds (`LENS_GARDEN_ENABLED`,
  quiet hours, `/quiet`, a pause, the welfare cooldown), out of
  character, with no `Outbound` row. The message is the gap line, up
  to 6 «• card text (domain)» lines (the rest counted: «в Inbox» writes
  every visible card), «скрыто: H» for high-risk cards, and «в Inbox»
  (`lg:a:<gap>:<epoch>`) / «не нужно» (`lg:x:`). A tap edits that same
  message with its outcome and removes the keyboard. A job that failed,
  found nothing or had every card hidden sends a short «ничего не
  нашлось», and the gap goes back to open, keeping
  `research_requested_at`: a gap is researched at most once, so a
  failed research is not a way to spend the quota twice. Cards left
  untapped expire (below); once none is left, the gap goes back to
  open the same way and the result message loses its buttons, so a
  gap is never stuck `researched`. The job's
  `offered_at` and the gap's `research_message_id` track the send; a
  crash between send and mark sends it twice, and the first copy's
  buttons are stale. A gap the garden closes while its research runs
  (the note now exists) is rechecked like an open one; its result is
  then dropped unsent, and the quota spent at the tap is not returned.
  The garden no longer delivers anything:
  `record_garden` only rechecks and resolves researched gaps, its gate
  is unchanged, and the report note shows statuses only, so no web
  text ever reaches `Anchor/Reports`. The spec's
  `lens_gap.research_run_id` was dropped with it.
- **The tap is one transaction, and the quota is spent there.** «N ·
  исследовать и написать» is its own row under a live `missing_note`, `tension` or
  `bridge` gap (not `link`: its fix is an edge between notes that
  exist), shown only while the gap was never researched and research
  can actually run: `RESEARCH_ENABLED`, `LENS_ENABLED`,
  `LENS_GARDEN_ENABLED`, `IDLE_ENABLED` and a non-empty `PACKET_LENS`
  (otherwise a tap would spend the day's quota on a job nothing runs).
  The tap moves the gap to `researched`, then queues a `kind='study'`,
  `packet='lens'` job with `/study`'s checks in `/study`'s order and
  its shared daily quota, and **no queue row**, so `/study`'s
  completion message cannot fire. A refusal («Исследования
  выключены.», the quota, the budget) rolls both back and the gap stays
  open; a stale tap is «Устарело». The callback grammar is
  `lg:(d|n|r|a|x):[0-9]+:[a-z2-7]{6}`, matched whole, at most 22 bytes.
- **The query is built from the gap and the lens only.** A new idle
  kind, `lens_research`, runs the job, so idle's preemption, budget and
  digest come with it and it cannot reach Telegram. Its query call sees
  exactly what `lens.gap_seed` loads: the gap's kind, detail and
  proposed title, and the titles and catalog summaries of the lens
  notes it names (the garden's model saw the same). Dialogs, memory,
  personal and knowledge notes are structurally absent, and the pure
  `app/research/lens_query.py` has an import allowlist. The query is
  validated (one line, no URL, no secret, no injection), charged to the
  job so `RESEARCH_JOB_USD_CAP` covers it, and never rebuilt; a refused
  one fails the job without searching. `find_urls` stays the only
  web-search call site. A lens job unfinished after 3 days fails as
  stale (idle may be off) and sends «ничего не нашлось».
- **Cards are `kind='lens'` and never a memory.** Distill keeps only
  claims that answer the query; the other Phase 4 layers (verbatim
  quotes, the injection list, the redactor, risk rules that only raise
  risk) stay. `/notes`, `/card`, `/adopt` and `/reject` never see lens
  cards, or a web `r:a:` press could turn a web page into a technique
  memory. A card expires `RESEARCH_CARD_TTL_DAYS` after its message
  went out, and twice that if it never went out.
- **«в Inbox» is one knowledge note per gap, written by one module.**
  `app/core/echo_write.py` is the only code that writes or undoes
  anything in the vault for Echo (an AST test pins it, and that it
  writes no memory). It renders one note holding every visible pending
  card (frontmatter exactly `anchor: knowledge`, `source_urls`, `gap`;
  the line «Исследование Echo; это не линза.»; `[[links]]` to the
  gap's lens notes; per card its text, verbatim quote and URL, all
  escaped as inert text), screens it, records an `echo_changeset` row
  (ids and times only) and commits it **before** the write, then calls
  vaultd. A lost answer is replayed with the same changeset id on the
  next tap, so one tap never makes two notes; a refusal deletes the
  row. The note is named after the proposed title for a missing note,
  so the garden's recheck finds it and resolves the gap; adopting moves
  the gap to done, which is your own act. «не нужно» rejects the cards
  and sends the gap back to open. Either way the garden message is
  re-rendered, so its item reads the truth («— записано в Inbox», or
  its row back without «исследовать и написать»).
- **vaultd enforces the writer, not the bot.** `PUT /v1/echo/inbox`
  takes a bare basename and builds the path itself, creates only (a
  taken name gets ` 2` to ` 9`), only in the inbox (`echo_inbox` in
  `Anchor/settings.md`, default `Echo/Inbox` when that conflicts with no
  rule; no settings file, no inbox), refuses any other frontmatter and
  `anchor: lens`, and stamps `anchor_edited_by: echo`. Echo's
  changesets carry `writer: echo` in the undo store, with their own
  caps (1 file per changeset, 20 a day, 4 undos an hour), which
  Claude's tunable caps never touch, and neither writer can undo the
  other's. Deploy vaultd before setting `echo_inbox`: an old vaultd
  rejects the unknown key.
- **`/lens undo`, Telegram only**, takes back the newest confirmed
  write under 14 days old: undone, nothing to undo, expired (vaultd no
  longer has it), or changed (you edited the note, and compare-and-swap
  refused, so your edit is never lost). It needs no switch: it only
  ever removes what Echo wrote. An undo whose answer was lost is not
  "changed" on the retry: vaultd's index marks it undone, and so does
  the bot. `/lens` adds «Исследования: идёт N, ждут решения M», M
  counting only results whose buttons are live.
- **Promotion is yours, and takes two steps (amends §9 and §14.5).**
  Move the note into a lens folder **and** change `anchor: knowledge`
  to `anchor: lens`. The mark alone is not enough (the inbox's
  knowledge rule outranks it), and neither is the move (the note's own
  mark keeps it out). Until then it is ordinary knowledge, which
  `search_library` and Claude's write tools can reach; Echo's own
  prompts read no knowledge (8d's block was never built), so
  unpromoted research reaches Echo only as anonymous graph structure.
  This is the gate §13 relies on: a poisoned page never becomes part of
  what Echo reasons with without you reading it.
- **Claude Code sees no text of it.** No new debug view or column (none
  for `echo_changeset`, and no `lens_gap_id`, which would tell
  `researched` from `resolved`, merged by `lens.gaps` into `closed`);
  the existing `debug.study_*` views show lens jobs and cards only as
  ids, kinds, statuses, costs, counts and domains. A gap going from
  `closed` back to `open` shows it was researched; that much is
  accepted. No log line names a gap alongside its research (never a
  gap id with a job id), and no lens function returns card text. Web
  text in the cards, the queries and the inbox notes stays with you.
- **Privacy, `/delete`, `/export`.** `/privacy` and `docs/privacy.md`
  say what a tap sends where (the gap and its notes' titles and
  summaries to the model, the query to Exa, pages only from
  `PACKET_LENS`) and that accepted results become knowledge notes you
  can undo for 14 days. `/delete` truncates `echo_changeset` and wipes
  vaultd's undo store, but the inbox notes stay: they are ordinary
  knowledge notes, yours from then on, and the confirmation now says so
  instead of promising every file. `/export` includes `echo_changeset`.
  Logs carry ids, counts and outcome codes; never a title, detail,
  query, card text, quote, URL, domain, note name, path or message id.
- **A lost answer never loses the note.** After a «в Inbox» whose
  answer from vaultd was lost (the note may exist, its changeset
  unconfirmed), a second «в Inbox» replays the same changeset first,
  from the cards that tap chose, even if they have expired since. A
  path no tap takes -- «не нужно», every card expired, the gap resolved
  by a recheck or gone -- asks vaultd's `GET /v1/changes` instead of
  writing: a note it holds is confirmed (so `/lens undo` can take it
  back, and «не нужно» answers that it was written), one it does not is
  forgotten, and a vault that does not answer is asked again on the
  next pass.

## L5 — reflection and critique read the lens

`anchor-lens-plan.md` rev. 3, milestone L5 (§7, §10, §12): the idle
reflect picks lens notes for itself and may rephrase its draft's open
threads on them, and critique records which lens notes stood behind the
replies it rated. The L5 spec settled the details; you approved its
three deviations from the plan and decided its open risks 2, 5 and 6
(below). The switch is `LENS_REFLECT_ENABLED`, off by default, on top
of `LENS_ENABLED`, as L3's garden switch is. This is the first path by
which lens ideas reach chat: a grounded thread is a notebook entry,
and the persona prompt shows the active entries.

- **Three deviations from the plan, owner-approved.**
  1. **The per-scene notebook stays lens-free**, although §10 and §12
     name it. It runs on raw dialogue several times a day; L2's shape
     would add two calls per scene with lens text next to that dialogue,
     and reusing another round's selection would record a claim nothing
     checked. Idle reflect writes the same `notebook_entry` rows, so it
     feeds the notebook instead. `run_notebook_reflect` and its prompt
     are byte-identical; its updates clear an entry's lens columns.
  2. **The reflect selector sees pass 1's validated draft, not "the
     window's scene summaries" (§7).** L2's rule that the selector never
     sees the week (above) wins: the lens calls get the draft only, which
     pass 1 built from input without welfare scenes, so the welfare
     exclusion needs no second copy.
  3. **Reflect rounds store no rationale.** It would be written from
     the week and no screen shows it. `record_round` refuses a
     `reflect` round with one; the selector's `why` is still asked for
     (the schema is shared with L2) and discarded.
- **Only open threads are grounded (your decision on the spec's risk
  2).** An observation is a fact about the user; rewritten through a
  lens it would attribute a lens idea to the user, and that sentence
  would then sit in the persona prompt as something Echo "knows". So
  the grounding call is shown only the draft's thread items (adds of
  kind `open_thread`, updates of an active thread), its prompt speaks
  only of threads, and the merge drops any rewrite that names
  anything else; the observation stands as pass 1 wrote it, with no
  lens ids. A draft with no thread add or update leaves the lens
  inactive for the run: no call, no round, the pre-L5 run byte for
  byte. The selector still sees the whole draft minus closes: which
  notes fit is a question about the notes as a whole. Eval case 47
  pins it.
- **Two single-shot calls after pass 1, the L2 shape.** Pass 1 is
  unchanged (its prompt's bytes are pinned; its prohibitions are
  factored out as `REFLECT_PROHIBITIONS` and open the grounding
  prompt). The selector reads the draft as JSON (`add[{ref, kind,
  text}]`, `update[{id, text}]`) and the catalog; the grounding call
  reads the thread items and §6's block, verbatim, under a strict
  schema with no `close` and no `kind`: it can only rephrase. The shared
  pieces (catalog and block renderers, selection checks, the budget)
  moved from `lens_review.py` to `app/core/lens_select.py`, which
  imports no vault, database, notebook, idle or review code; L2's
  suite passes unmodified. L2's savepoint, cap and commit sequence
  stays its own: a shared `call()` would have rewritten it for no gain.
- **The merge is in code, by `ref`.** At most 3 rewrites
  (`GROUNDED_MAX`, to stay inside the shared safety provider's
  400-token output cap); an add's `ref` must be one of the offered
  thread refs, used once, and keeps the draft's kind; an update's id
  must be one of the offered thread updates; `grounds` must name a
  selected title (resolved to `lens_note_ids`); the text must pass
  `notebook.validate` on its own (length, `screen()`, Anchor-only id);
  and a casefolded leak guard drops a text holding a selected title its
  draft item lacked, or an 8-word run of a selected body. A dropped
  rewrite leaves its draft item as it was. Closes are exactly the
  draft's. The lens may rephrase a pass-1 thread, never drop, swap or
  add one. A reply of the right shape is `grounded` even if every
  rewrite in it was dropped, as in L2.
- **The lens never fails reflect.** A provider error, a malformed
  reply or `JobCapHit` on either call keeps the draft and records
  `fallback` (with the selection's ids if the selector had answered);
  anything else keeps the draft and records no round. Every call goes
  through the run's `RunContext.charge` (`idle:reflect`), so
  `IDLE_JOB_USD_CAP` covers pass 1 and both lens calls; the gate
  already reserved it against both daily caps. `charge` writes its
  ledger row before raising, so that row is committed -- and so, now,
  is pass 1's own on `JobCapHit`, which used to roll back. Preemption
  is checked before any lens spend and again, as before, before the
  apply; the round and the entries that name it commit together after
  it, so a preempted run records no round.
- **Storage** (migration `3d3efa0cbc9a`). `lens_round.consumer` allows
  `reflect`; `lens_round.idle_run_id` points at the run **ON DELETE SET
  NULL**, not CASCADE, following `lens_garden_run.idle_run_id`: a round
  must outlive its run, since rotation is computed from rounds and the
  rounds are §13's audit trail. `ck_lens_round_link` ties a review
  round to no run and a reflect round to no review.
  `notebook_entry.lens_round_id` (SET NULL) and `lens_note_ids`
  (`int[]`, default `'{}'`) say which round a grounded entry came out
  of and which notes it rests on -- ids, not a foreign key, as on
  `review_proposal`. `/export` carries them; `lens_round` stays out.
- **Rotation is per consumer.** `rounds_since_used` counts only the
  consumer's own rounds, both ways: reflect may run daily and the
  review weekly, so counting them together would let a week of reflect
  rounds age every note the review picked, and «не выбирали 4 раунда»
  would stop meaning four weeks. `/lens` shows the review's last round
  as before and, once one exists, «Последняя рефлексия с линзой».
- **Undo compares only what the snapshot logged.** `idle_change`
  snapshots taken before the migration lack the two new columns;
  without the fix, undoing any reflect run from before the deploy would
  report every change as a conflict for the 7 days of
  `IDLE_UNDO_DAYS`. A restore still writes back only the keys in
  `before`, and new snapshots include the lens columns, so undo
  restores them.
- **Critique attributes, it does not select.** No call, no catalog, no
  lens text, no `app.vault.lens` and no `lens_select` (§10). For each
  sampled reply, at the reply's time, it collects the lens ids behind
  the grounded changes that were in the persona prompt then: adopted
  amendments (live from activation to revocation), standing orders
  from a review proposal (from decision to retirement; a
  counter-proposal is the user's own text and has no link) and
  notebook entries (from their last update to closing -- an entry
  updated after the reply is missed rather than misattributed). The
  union, first-seen, and the number of replies with a source go into
  `idle_run.summary` as `lens_note_ids` and `lens_grounded`, only with
  `LENS_ENABLED` on and something grounded; otherwise the summary and
  the digest are byte-identical. Neither key is in `SAFE_EXTRA_KEYS`,
  so the runner's log never shows note ids; the ids reach the user in
  `/export` only. Reflect's summary gains `lens_round_id` (logged) and
  `lens_outcome` (not logged).
- **Turning the lens off leaves grounded threads in place (your
  decision on risk 5).** They expire on their own TTL like any thread,
  or close when resolved, just as adopted grounded amendments stay.
  `/privacy` says so.
- **`lens.rounds(n)` keeps its signature (your decision on risk 6).**
  It already returns each round's consumer, so reflect rounds show up
  there with their picks and outcome and no rationale; daily reflect
  rounds will push review rounds out of its 50-row window sooner, which
  is accepted. `debug.lens_round` is unchanged (its column list leaves
  out `idle_run_id`), and `idle_run` still has no debug view.
  `docs/claude-access.md` and CLAUDE.md say "each weekly review or
  reflection round".
- **Guards.** `tests/test_idle_isolation.py` bans
  `app.core.lens_select` from idle code and allows it, with
  `app.vault.lens`, in `reflect_lens.py` alone -- reflect's one door to
  the lens; reflect.py and critique.py get no entry. The mock-bot run
  of `reflect` now runs with the lens active and still sends nothing.
  `tests/test_vault_notes_isolation.py` adds `reflect_lens.py` alone to
  the lens module's importers, still refuses reflect.py, critique.py and
  notebook.py, and checks lens_select.py reaches no `app.vault` module.
  `tests/test_autonomy_isolation.py` lists lens_select.py, which writes
  nothing.
- **Eval.** A `lens_reflect` case kind runs `reflect_lens.run()` and
  `record()` over a draft the case supplies (through the real
  `notebook.validate()`), with checks on the outcome, the selection,
  the grounds, no title in an entry's text, and the draft's shape kept
  (observations word for word), and five judge items worded for
  notebook entries, since L2's speak of proposals. Cases 46-50: a
  fitting note; a note inviting a user trait, next to observations
  that must stay untouched; an acceleration note; an injection asking
  to add an intention and close every thread; no fit, `empty`. All
  non-blocking.

## `/lens garden now`

The garden (L3) runs only as idle work, so the first one could take
days: it waits for the user to be quiet for `IDLE_AFTER_H`, inside
`IDLE_WINDOW`, and then only if the lens changed. `/lens garden now`
queues one run on demand through its own gate,
app/core/idle/gate.py's `manual_garden_gate`, rather than weakening the
idle one.

- **Dropped**, because they exist to keep idle work out of the user's
  way and the user asked: `user_active` (the command is itself a
  message), `window`, the 168-hour interval, `unchanged`, and
  preemption (a message during the run would otherwise throw the run
  away).
- **Kept**: the switches, the pause, the welfare cooldown, `busy`,
  every money row (jobs per day, the idle cap, the reserve), the lens
  size, and **once per local ISO week**. `lens_garden_run.iso_week` is
  UNIQUE, so a second run in a week could not record its gaps; the
  reply says the next one is next week.
- The run is an ordinary `lens_garden` idle run whose job payload
  carries `manual: true`; app/core/idle/runner.py re-checks it against
  the manual gate. Nothing about the automatic path changed. The
  garden's Telegram message keeps its own holds (quiet hours, pause,
  welfare), so a run started at night reports in the morning.

