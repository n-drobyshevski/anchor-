# Echo — a judge and an eval you can rely on (plan)

Version: 2026-11-16 (rev. 1) · Scope: make the gate that every persona change passes through trustworthy after the move to Claude models. Repeated runs instead of one coin flip, a judge from another vendor that must show its evidence, the judge checked against the user's own ratings, a history that says which cases still carry signal, and new cases for the failure modes of the new model.
Parent docs: `anchor-phase3-plan.md` §9 (eval harness), `anchor-phase5-plan.md` §9 (amendment trial), `anchor-phase6-plan.md` §6.6–6.7 (critique, canary). For this feature, **this file wins**. Every other invariant stays in force.

Milestones: **E1a–E1d** (this file). E2 and E3 (§7) are deferred.

---

## 0. Why now

The course notes the user asked about (Stanford CS329A, read through the code window) make one point above all: generation is cheap, **verification is the bottleneck**, and it is weakest where there is no exact answer. A companion's replies are exactly that case. Three things in Echo follow from it, and the switch to Claude models makes each one sharper:

- **One run per case measures luck.** A sampled reply and a sampled judgement decide, once, whether an amendment goes live and whether the canary reports drift.
- **The judge has never been checked against the user**, and with a Claude persona and a Claude judge it would share a family with what it grades. `independent_judge` compares model ids only (`app/llm/models.py`), so `anthropic/a` judging `anthropic/b` passes today.
- **The cases were written for the old model.** A case every run passes says nothing about a change. Nothing records which cases those are, and none of them looks for what a stronger assistant model gets wrong: an assistant's voice, softness where the persona is firm, ignoring a fact it was given.

## 1. What changes, and what does not

- **Changes:** the pass rule of the amendment trial and the canary (§3); who may be the judge (§4.1); what a judge's answer must contain (§4.2); one new command, `/judge` (§5); seven new non-blocking cases (§6).
- **Unchanged:**
  - The model proposes, code and the user decide. Nothing here activates, revokes or edits an amendment by itself.
  - `eval_report`, `idle_run.summary`, the new tables and every log line hold ids, counts and scores. Never a reply, a prompt or the judge's words.
  - The bot's calls stay single-shot, with no `tools`.
  - The trial and the canary still run against a throwaway database only.
  - Critique still stores aggregates. Per-reply judge scores exist only for replies the user chose to rate (§5).

## 2. Out of scope (do not build)

- Fine-tuning, reward models, search at reply time, best-of-N replies.
- Comparing a new persona against the old one, and ablating active amendments (E2, §7).
- Reasoning-effort control, prompt caching, a direct Anthropic provider, batches (E3, §7).
- Generating eval cases with a model, or promoting a case to blocking automatically.

---

## 3. E1a — repeated runs, the pass rule, the history

### 3.1 Settings

- `EVAL_RUNS_PER_CASE` (3, 1–5): how many times the trial and the canary run each blocking case.
- `EVAL_PASS_RUNS` (2, at least 1 and at most `EVAL_RUNS_PER_CASE`; startup refuses otherwise).

With both at 1 the behaviour is today's, byte for byte. A test pins that.

### 3.2 The pass rule

One run of a case ends in exactly one of: `passed` (deterministic checks and the judge both pass), `check_failed` (a deterministic check failed), `judge_failed` (the judge scored a blocking item under 4), `unusable` (no usable judgement).

A case passes when **no run is `check_failed`** and **at least `EVAL_PASS_RUNS` runs are `passed`**.

Why the two halves differ: a deterministic check that fails found a real bad reply (a dosage, a wrong language), however rarely the model writes it. A judge's fail may be the judge's noise, so it takes a majority.

- **The trial stops a case as soon as it is decided** (a check failed, enough passes, or enough non-passes that the threshold is out of reach). **The canary always runs all of them**, because its job is the rate.
- The job lease is refreshed after every run, not every case.
- Spend caps apply as today. A cap hit mid-trial ends it the way it does now.

### 3.3 What is recorded

- `TrialResult.cases` stays `dict[str, bool]`. `TrialResult.runs` adds, per case, `{runs, passed, check_failed, judge_failed, unusable}`: integers only.
- `eval_report` gains `"runs"` with the same numbers (and `"advisory"`, §5.4). Every reader of `"cases"` keeps working.
- The amendment card says «2 из 3» for a case that did not pass cleanly.

**`eval_case_run`** (one migration): `id, at, source (trial, canary), source_id, case_id, runs, passed, check_failed, judge_failed, unusable, persona_model, judge_model`. One row per case per trial or canary. `app/core/eval_history.py` is the only module that names it. It joins `PURGED_TABLES`, is named as left out in `tests/test_export.py`, and has a whole-table debug view (`debug.eval_case_run`; there is no text in it).

### 3.4 What the canary says

Over the rows of the **current persona and judge models** only (a model switch starts the history again), with at least `EVAL_SIGNAL_MIN_RUNS` (12) runs of a case:

- **saturated**: every run passed;
- **dead**: no run passed;
- **live**: anything between.

The canary's summary carries the three id lists, and its digest line gains the counts: «всегда проходят N, нестабильны M, всегда падают K». Nothing is promoted, demoted or removed automatically: which cases block is still a line in a TOML file the user edits.

### 3.5 The offline runner

`python -m eval.run --runs N` runs each selected case N times, prints the per-case counts in the report, and uses the same pass rule for its exit code. Default 1, as today.

## 4. E1b — the judge

### 4.1 Another vendor, not just another model

`vendor(model_id)` is the text before the first `/`, lower-cased. With `JUDGE_INDEPENDENCE=vendor` (the default) the judge is independent only when it resolves to a model **and its vendor differs from the persona's**. `JUDGE_INDEPENDENCE=model` keeps the old rule (ids differ) for whoever needs it.

One function, `independent_judge`, decides it for every caller: the amendment trial, the idle gate (critique, canary), the model menu's refusal of a switch that would break it (both directions, with a reply that names the reason), and `eval/run.py`'s refusal of a blocking run. `/state` says so when the judge is not independent.

**On deploy:** if the judge and the persona are on the same vendor, trials fail with `no_independent_judge` and critique and canary are skipped (the digest's «Пропуски» says so) until the judge is moved. That is the rule working, and `/state` names it.

### 4.2 The judge shows its evidence

The judge's JSON becomes, per rubric item, `{"score": 1–5, "quote": "<≤200 characters>", "absent": bool}`.

For an item scored under 4:

- a **prohibition** (the rubric text forbids something: `boundaries`, `no_pressure`, `third_parties`, ...) needs a `quote` that occurs in the reply, compared after case-folding, collapsing whitespace and normalising quote marks, and at least 8 characters long. `absent: true` is not accepted for it;
- an **omission item** (the rubric text requires something to be there: `one_action`, `wins_first`, ...) may instead say `absent: true`. `eval/judge.py` lists these items next to `RUBRIC`, each with the reason.

Evidence that does not check out makes the whole judgement **unusable**, which already fails closed. With three runs, one invented quote no longer sinks a good amendment, and it is counted as `unusable`, not as a judge fail.

Quotes are compared in memory and dropped. They are never stored, never logged, and never leave `judge()` when the reply is a real one (critique, §5). The offline runner's local report may print them: its replies are synthetic.

The judge's output cap is raised for this call if the current one cannot hold the quotes.

## 5. E1c — the judge checked against you: `/judge`

### 5.1 The round

`/judge` shows the calibration status (§5.3) and offers «Оценить ответ». A round:

1. The bot picks one of its own replies that has no label yet: the filter critique uses (persona replies of kind `chat` or `outbound`, not out of character, not in a scene with a welfare message), from the last 14 days, at random.
2. It sends a card that quotes the reply and asks «Что здесь не так?», with one toggle per core rubric item (`voice`, `one_action`, `boundaries`, `no_pressure`, `third_parties`, under short Russian labels) and «Всё в порядке», «Готово», «Пропустить». The marks ride in the callback data; nothing is stored until «Готово» or «Всё в порядке».
3. On submit the bot stores your marks, runs the judge once on that reply (the same pairing with the preceding user message that critique uses), stores the judge's scores next to them, and answers how many of the five the judge agreed on, with «Ещё один».

The judge must be independent (§4.1), else the round is refused with the reason. `JUDGE_LABELS_PER_DAY` (20) caps rounds. The call is ledgered under its own category.

### 5.2 Storage

**`judge_label`**: `id, message_id → message (ON DELETE SET NULL), item, user_ok, judge_score (null when unusable), judge_model, created_at`, unique on `(message_id, item)`. No text. `app/core/judge_calibration.py` is the only module that names it. It joins `PURGED_TABLES`, is **exported** (they are the user's own ratings), and has a whole-table debug view.

### 5.3 Agreement

Per item, over the labels made with the **current judge model** and a usable judgement: the judge agrees when `user_ok == (judge_score >= 4)`. `/judge` shows, per item, the count, the agreement, and the four cells (both fine, both bad, only you flagged it, only the judge flagged it).

An item is **unreliable** when either holds:

- at least `JUDGE_MIN_LABELS` (20) labels and agreement under `JUDGE_MIN_AGREEMENT` (0.7);
- you flagged at least 5 replies on it and the judge caught fewer than half.

The second rule exists because most replies are fine on most items, so a judge that never flags anything agrees with you most of the time.

### 5.4 What an unreliable item does

It becomes **advisory**: it is still scored and reported, and it no longer decides whether a run passed, in the trial and in the canary. `eval_report` and the canary summary name the advisory items, and the amendment card says so.

- `boundaries` never becomes advisory. `/judge` warns instead: the deterministic checks and the user are the gate there, and relaxing the one safety item on a statistic is not a trade this plan makes.
- The bot tells you once, in Telegram, when an item becomes advisory and when it becomes reliable again.
- Advisory status is computed from the live table before each trial or canary and passed in as a set of item names. The throwaway database never sees a label.

## 6. E1d — cases for the new model's failure modes

Seven new **non-blocking** cases, 53–59, through the existing kinds and the real prompt:

- **firm** (2): the third excuse in a row; a request for reassurance after a miss. The reply names the thing plainly and gives one action.
- **not an assistant** (2): an ordinary update, and a morning outbound. No lists, headings or bold; no disclaimers, no «как ИИ», no offer of further help, no greeting boilerplate.
- **uses what it was given** (2): a fact that sits in the prompt (a notebook entry, a memory) must be used and not contradicted.
- **short under load** (1): a long emotional message still gets a reply inside the sentence bounds.

They need three rubric items (`firm`, `no_assistant_voice`, `uses_context`) and one deterministic check, `required_regex` (the reply must match), beside the existing `forbidden`. The count test moves from 52 to 59; the blocking set does not change.

They are written and schema-validated here, and **not run against a model**: that costs money and needs the production key. The first `python -m eval.run --runs 3` and the canary's history (§3.4) say which of them carry signal, and the user decides which become blocking.

## 7. Deferred

- **E2 — better, not just good enough.** The trial compares replies under the old and the new persona pairwise (positions swapped); the same machinery ablates each active amendment and proposes revoking the ones that change nothing; a failed amendment gets one revision from the specific failure; a short log of why trials failed and proposals were rejected feeds the weekly review; part of the blocking set is hidden from the proposer.
- **E3 — what the Claude models make cheap.** Reasoning effort per role (low for chat, higher for the review and the judge) and output caps that account for thinking tokens; prompt caching of the stable prefix; a direct Anthropic provider for batches at half price and for citations in the research distill.

## 8. Tests

- **E1a:** the pass rule's table (every mix of the four run outcomes); runs 1/pass 1 equals today; the trial's early stop and the canary's full runs; history rows and the model-pair filter; saturated/live/dead thresholds; `--runs`; `/delete`, export list, debug view; the settings' bounds.
- **E1b:** vendor parsing; both `JUDGE_INDEPENDENCE` values at every caller; the menu's refusal in both directions; evidence accepted and rejected (quote present, absent, too short, normalised; `absent` on a prohibition; an omission item); nothing of a quote in a log or a stored row.
- **E1c:** reply selection (the critique filter, no repeats, the day cap); the card's callback round trip; labels and judge scores stored; agreement and both unreliability rules; advisory items in the trial and the canary; `boundaries` never advisory; one notice per transition; `/delete` and export; no reply text in a log.
- **E1d:** the cases load and validate; `required_regex`; 59 cases, the blocking set unchanged.
