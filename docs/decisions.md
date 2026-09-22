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

`anchor-phase1-plan.md` through `anchor-phase4-plan.md` remain the
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
