# Echo: suggested interest topics (`/interests suggest`)

Status: built (migration a5d1c8e3f702). Follows `/interests run` (docs/decisions.md → `/interests run`).

## 1. Why

Research topics come only from the user (anchor-phase6-plan.md §1, "Out of scope: autonomous topic choice for research"; the `InterestTopic` docstring). The owner wants the bot to *propose* topics, while keeping the decision with the user.

This needs an **owner amendment** to that rule. The model may suggest topics; only a tap on a suggestion's button creates an `interest_topic` row. Nothing is searched until the user adds a topic, and either taps «добавить и искать», runs `/interests run`, or idle research reaches it. The amendment is recorded in docs/decisions.md, `## Suggested interest topics`.

## 2. User surface

- **`/interests suggest`.** Telegram only. On the web chat it replies `WEB_ONLY_REPLY`, like `/lens`.
  - It replies at once: «Подбираю темы. Пришлю, когда будут.»
  - Then one message arrives:

    ```
    Можно поискать:
    1. «<тема>» [forums] — <почему, одной строкой>
    2. …
    ```

  - Each suggestion has one keyboard row: `[N · добавить] [N · добавить и искать] [N · ✖]`.
- **Buttons.** Callback data is `is:<a|r|x>:<suggestion_id>:<epoch>`.
  - `a`: add it as a topic. Toast «Добавлено: «…».» The row becomes `N · ✓`.
  - `r`: add it, then `enqueue_interest(topic_id=new id)`, the same path as `/interests run`. The manual quota (`RESEARCH_MANUAL_PER_DAY`) and the money caps still apply. If the run is refused, the topic stays added and the toast says why.
  - `x`: dismiss it. The row becomes `N · ✖`. The model is told not to offer it again.
  - Stale or replayed taps get «Устарело.», as in app/tg/research.py.
  - Topic-cap and risk-screen refusals reuse `app/core/interests.add_topic`'s codes and texts (`CAP_TEXT`, `REFUSAL_TEXT`), and the suggestion stays open.
- **Refusals for `/interests suggest`:**

  | Condition | Reply |
  |---|---|
  | `RESEARCH_ENABLED` is off | «Исследования выключены.» |
  | No topics and the lens is not active | «Не из чего предлагать: добавь тему через /interests add.» |
  | The daily suggestion quota is used | «На сегодня предложений хватит.» |
  | `check_cap` | «На сегодня бюджет на исследования исчерпан.» |
  | A batch is already being made | «Уже подбираю.» |

- **An unsent batch is re-sent, not re-made.** A finished batch may not have gone out (quiet hours, a pause, `/quiet`; app/core/report.py `may_report_now`). The next `/interests suggest` sends that batch instead of paying for a new one.
- **Usage line.** `/interests` usage and the `LIST_HINT` neighbourhood gain «Предложить темы: /interests suggest.».
- **Menu.** An optional menu button «Предложить темы» (app/tg/menu.py `ACTIONS`, gated on `RESEARCH_ENABLED`). It takes no argument, so it is allowed there.
- **Weekly, unasked (the owner's choice).** A new idle kind `interest_suggest` makes one batch a week. Its message goes out through the same send hook as a manual batch, with the same buttons. This is the lens garden's shape: an idle job writes, and a worker hook sends when `may_report_now` allows. So it needs no new exception to "idle never messages" beyond the garden's own. Section 5a has the rules.

## 3. What the model sees and what it never sees

**Input**, built in a new `app/research/suggest.py`. It has an import allowlist test, like `lens_query.py` in tests/test_research_isolation.py.

- All `interest_topic` rows, active and retired, as text and packet. These are the user's own typed topics, which already go to search.
- The last 30 dismissed suggestions' texts, as "do not offer again".
- **Only when `lens.lens_active`:** lens note titles and summaries from `app/vault/lens.catalog(...)`. The garden and the lens query already send exactly these to the same provider. They are capped by `INTEREST_SUGGEST_MAX_CHARS`, 6000 by default.
- The configured packets that have sites, with a one-line meaning each (forums, guides, ref). An empty packet is never offered.

**Never sent:** dialogs, memory, journal, notebook, summaries, check-ins, personal or knowledge notes, or study cards. These are structurally absent: `suggest.py` does not import those modules, and the isolation test pins that.

**Output:** strict JSON under `SUGGEST_SCHEMA`: `{"suggestions": [{"text", "packet", "why"}]}`, with at most `INTEREST_SUGGEST_MAX` items (default 5). The call is single-shot, with no tools and no web search. `find_urls` stays the only search call site (tests/test_web_search_isolation.py).

**Validation in code, before anything is stored:**
- `text` is 3–100 characters (`interests.TEXT_MAX`) and `why` is at most 160.
- `packet` must be in `PACKETS` and have sites.
- No URLs, domains or `site:`.
- `redact.find_secret`, `injection.is_clean` and `interests.screen` must pass.
- A suggestion is dropped if it matches an existing topic or a dismissed suggestion (casefolded, whitespace collapsed), or repeats another in the batch.
- A batch with nothing left is `done` with 0 suggestions and sends «Новых тем не нашлось.»

**Model:** `INTEREST_SUGGEST_MODEL`, which falls back to `LLM_MODEL_SAFETY` when empty. It is built like `lens_garden.build_garden_provider` (temperature 0.3, `INTEREST_SUGGEST_MAX_TOKENS` 1200). Spend goes to `spend_ledger` under category `interest_suggest`.

## 4. Data

One migration with two tables:

- **`interest_suggest_batch`** with columns:
  - `id`
  - `status` (`queued|done|failed`, CHECK)
  - `error_code`
  - `local_date`
  - `usd_cost`
  - `epoch` (6 base32 characters, as in the garden)
  - `message_id`
  - `sent_at`
  - `created_at`
  - `finished_at`
- **`interest_suggestion`** with columns:
  - `id`
  - `batch_id` (FK, cascade)
  - `position`
  - `text` (≤100)
  - `packet` (CHECK in forums|guides|ref)
  - `why` (≤160)
  - `status` (`open|added|dismissed|expired`, CHECK)
  - `topic_id` (FK `interest_topic`, SET NULL)
  - `created_at`
  - `decided_at`
- **Expiry.** A new batch marks every older `open` suggestion `expired`, so old buttons go stale. No sweep is needed.
- **Debug views.** Content-free columns only:
  - `debug.interest_suggest_batch`: `id, status, error_code, local_date, usd_cost, created_at, finished_at, sent_at`.
  - `debug.interest_suggestion`: `id, batch_id, position, status, created_at, decided_at`.
  - Text, packet and why are never in a view. tests/test_debug_views.py's `CONTENT_COLUMNS` gains the new text columns.
- **`/export` and `/delete`.** Both tables are added to app/core/export.py and app/core/purge.py, with their FK order noted in purge's docstring.

## 5. Flow

1. Router `interests_command` gets a `suggest` branch. Its order:
   1. the `is_web_sink` guard;
   2. the `RESEARCH_ENABLED` check;
   3. `_once`;
   4. `interests_ui.run_suggest`, which calls `app/research/suggest.enqueue_suggest(session, settings, clock, *, timezone)`. In order, that function:
      - re-sends an unsent done batch if there is one;
      - refuses if the user has nothing to suggest from;
      - refuses if a batch is already queued (busy);
      - checks the quota (`INTEREST_SUGGEST_PER_DAY`, default 2);
      - runs `check_cap`;
      - inserts the batch and `enqueue_job(INTEREST_SUGGEST, {"batch_id"}, dedup_key=f"suggest:{id}")`.
2. The worker branch `kind == INTEREST_SUGGEST` calls `suggest.run_batch(...)`:
   - it builds the input, makes one call, validates, inserts the suggestions and expires older open ones, all in one transaction;
   - it then calls `_send_interest_suggestions`, gated by `may_report_now`, which sends the message, stores `message_id` and `sent_at`, and logs counts only.
3. Callback `is:` → `interests_ui.handle_suggestion_callback`. One transaction:
   - a conditional `UPDATE interest_suggestion SET status=… WHERE id=:id AND status='open'` joined to the batch on `epoch`, so 0 rows means «Устарело.»;
   - then `interests.add_topic` (`a`/`r`), whose refusal rolls back the whole tap;
   - commit;
   - for `r` only, `enqueue_interest` after the commit, so a refused run never undoes the add;
   - finally the message is re-rendered by edit only, the same rule as `/notes`.
4. **Sending, one path for both triggers.** `_send_interest_suggestions(sessionmaker, bot, settings, clock)` sends the newest done, unsent batch (if any), gated by `may_report_now`. The worker calls it right after an `INTEREST_SUGGEST` job and after every idle run, and the heartbeat calls it each tick (one cheap query), so a batch held by quiet hours goes out once they end. A batch still unsent after 48 hours expires unsent, and its suggestions go to `expired`.
5. Web ingress: `is:` is added to `BLOCKED_CALLBACK_PREFIX`. The message is sent only to Telegram, as the lens garden's is, because its text can be drawn from lens notes and a tap can start paid search.

## 5a. The weekly idle kind `interest_suggest`

- **Placement.** Its spot in `PRIORITY` (app/core/idle/planner.py) is right after `LENS_GARDEN`, so the garden's new gaps exist before suggestions are drawn from the same lens notes.
- **Gate.** The shared idle rows (`user_active`, window, pause, welfare cooldown, busy, max jobs, the idle cap, the reserve), then `_interest_suggest_rule`, which checks in order:
  - `RESEARCH_ENABLED`, then `INTEREST_SUGGEST_WEEKLY`;
  - at least one active topic, or an active lens; otherwise `nothing_to_suggest`;
  - no batch of either kind in the last 6 days (`suggested_recently`);
  - not `undecided`: the latest batch still has `open` suggestions and is under 14 days old. Unanswered suggestions are not met with more.
  - `KIND_DAILY_MAX[INTEREST_SUGGEST] = 1`.
- **Body.** `app/core/idle/interest_suggest.py` inserts a batch with `origin='weekly'` and calls the same `suggest.run_batch`. Spend is folded into `idle_run.usd_cost` (`IDLE_USD_CAP`) and ledgered under `interest_suggest`. It is preempted like the other idle kinds before the call, and never after the batch is stored.
- **Quota.** A manual `/interests suggest` does not count weekly batches against `INTEREST_SUGGEST_PER_DAY`, and a recent manual batch does push the next weekly one back (`suggested_recently`).
- **Data.** `interest_suggest_batch.origin` is `manual|weekly` (CHECK), and the debug view shows it.
- **`/digest`.** One line for the weekly run, as a count only.

## 6. Config (`app/config.py`, `.env.example`)

| Setting | Default | Notes |
|---|---|---|
| `INTEREST_SUGGEST_PER_DAY` | 2 | |
| `INTEREST_SUGGEST_MAX` | 5 | range 1–8 |
| `INTEREST_SUGGEST_MAX_CHARS` | 6000 | |
| `INTEREST_SUGGEST_MODEL` | `""` | |
| `INTEREST_SUGGEST_MAX_TOKENS` | 1200 | |
| `INTEREST_SUGGEST_WEEKLY` | true | the weekly idle batch; it also needs `RESEARCH_ENABLED` and `IDLE_ENABLED` |

There is no separate on switch: the feature lives under `RESEARCH_ENABLED`, and the lens input under the lens switches already in place.

## 7. Docs

- **docs/decisions.md:** the owner amendment and the design above. It also records:
  - Dropped: the "topics only from the user" rule, for suggestions only.
  - Kept: a topic exists only after a tap, and personal data never reaches the call.
  - The weekly batch follows the garden precedent: an idle job writes it and a hook sends it.
  - Not done: suggestions drawn from anything but topics and lens notes.
- **docs/privacy.md:** what the suggestion call receives, and that it goes to OpenRouter only, never to search.
- **README:** the `/interests` row and the settings.
- **docs/claude-access.md:** the two debug views.

## 8. Tests

- **`tests/test_interest_suggest.py`** (scripted provider):
  - the input contains topics, dismissed texts and lens titles/summaries only when the lens is active, and nothing else;
  - validation drops bad packets, URLs, duplicates of topics and of dismissed suggestions, over-long text and risk-screened text;
  - the batch is stored and older open suggestions are expired;
  - a zero-suggestion batch;
  - the ledger row;
  - each `enqueue_suggest` refusal, plus re-send of an unsent batch.
- **`tests/test_interests_commands.py`:**
  - `suggest` on the web chat;
  - disabled;
  - the reply on success;
  - each tap (`a`, `r`, `x`);
  - a replayed tap gives «Устарело.» and leaves one topic;
  - a stale epoch;
  - a cap refusal keeps the suggestion open;
  - `r` with the manual quota used still adds the topic.
- **tests/test_research_isolation.py:** the `suggest.py` import allowlist and its log keys.
- **tests/test_web_search_isolation.py:** unchanged, and must still pass.
- **tests/test_debug_views.py, tests/test_export.py and tests/test_delete.py:** the new tables.
- **tests/test_idle_interest_suggest.py:**
  - each gate reason, including `suggested_recently` and `undecided`;
  - the batch has `origin='weekly'`;
  - it is preempted before the call;
  - the send hook holds the batch in quiet hours and sends it after;
  - a batch unsent for 48 hours expires.
- **A migration test** modeled on tests/test_study_job_manual_migration.py.
- **An eval case** in `eval/` for the suggestion call, dry-run only in CI: it checks that the model offers no personal-sounding topics when given only lens summaries.

## 9. Owner decisions (2026-10-24)

1. Input: topics plus lens note titles and summaries when the lens is active.
2. Buttons: «добавить», «добавить и искать», ✖.
3. Trigger: `/interests suggest`, and also weekly on its own (section 5a).
