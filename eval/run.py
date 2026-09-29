"""The eval runner (phase-3 plan section 9).

    python -m eval.run              # all cases
    python -m eval.run --case 04    # one, by id prefix
    python -m eval.run --dry-run    # build every prompt, call nothing

**Manual only. Never in CI.** It hits the real API and costs about
$0.10 a run. Section 9's rule: run it before any `persona.md` edit or
model change is deployed, and a failure in a blocking case stops the
change.

Exit codes: 0 all good, 1 a blocking case failed, 2 only non-blocking
cases failed, 3 the run was refused because the judge is the model under
test. The 1/2 split matters because non-blocking failures are
information -- Cydonia drifting a sentence over on case 3 is worth
seeing and is not worth halting a deploy for. 3 is separate from both
because such a run did not fail; it did not *mean* anything, which is
worse, since a green report is what someone would quote to justify
shipping. Override with --allow-same-judge when you know what you are
reading.

Reports go to `eval/reports/<timestamp>.md` and are committed, so the
history of how the persona behaved is in the repo next to the persona.

L2 (anchor-lens-plan.md section 13): a `lens_review` case runs the
weekly review's lens round rather than a persona reply, on a third
provider built the way app/main.py builds the review's own
(`LLM_MODEL_SAFETY`, its temperature and token cap, structured
outputs). Its "reply" is the round rendered for the report; the text
checks run over the proposals' own words and the lens checks over what
the round did (eval/checks.py's `lens_checks`).

L3 (the L3 spec section 9): a `lens_garden` case runs the lens garden's
step 1 and its one call (`lens_garden.propose`) on the provider the idle
kind builds for itself (`lens_garden.build_garden_provider`: the safety
model at temperature 0 with `GARDEN_MAX_TOKENS`). Its "reply" is the
validated gaps rendered for the report; the text checks run over the
gaps' own words -- and over the raw reply, before `validate()` and its
`screen()` drop anything -- and the garden checks over which gaps
survived (eval/checks.py's `garden_checks`).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime
import pathlib
import sys

from app.config import Settings, get_settings
from app.core.clock import SystemClock
from app.core.extract import parse_json
from app.core.idle import lens_garden
from app.llm.openrouter import OpenRouterProvider, build_client
from eval import checks as checks_module
from eval import judge as judge_module
from eval import scenario
from eval.cases import LENS_GARDEN, LENS_REVIEW, Case, load_all
from eval.db import throwaway_sessionmaker

REPORTS_DIR = pathlib.Path(__file__).parent / "reports"


@dataclasses.dataclass
class Outcome:
    case: Case
    reply: str
    check_results: list
    verdict: judge_module.Verdict
    usd_cost: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error is not None:
            return False
        return all(r.passed for r in self.check_results) and self.verdict.passed

    @property
    def failures(self) -> list[str]:
        out = [r.name for r in self.check_results if not r.passed]
        if not self.verdict.usable and self.case.judge_items:
            out.append("judge_unusable")
        out.extend(self.verdict.failed)
        return out


# H5: exit code 3, distinct from 1 (a blocking case failed) and 2 (only
# non-blocking failures). A run whose judge is the model under test did
# not fail -- it did not mean anything, which is worse, because a green
# report is exactly what someone would quote to justify shipping.
EXIT_SAME_JUDGE = 3


def judge_model_for(settings: Settings) -> str:
    """The judge model, falling back to the cheap one as section 9 says."""
    return settings.LLM_MODEL_JUDGE or settings.LLM_MODEL_CHEAP


def same_judge_warning(judge_model: str, settings: Settings) -> str | None:
    """A loud line when the judge is the model it is grading, else None.

    Shared by the dry run and the real one so the warning cannot drift
    between them -- a dry run is where someone would check the setup
    before spending money, and it is the cheapest place to notice.
    """
    if judge_model != settings.LLM_MODEL:
        return None
    return (
        "\n"
        "!!! " + "=" * 68 + "\n"
        f"!!! ПРЕДУПРЕЖДЕНИЕ: судья и оцениваемая модель совпадают ({judge_model}).\n"
        "!!! Модель оценивает собственные ответы. Оценка «пройдено» здесь\n"
        "!!! не значит ничего: она не независима.\n"
        "!!! Задай LLM_MODEL_JUDGE другой моделью, или запусти с\n"
        "!!! --allow-same-judge, если ты понимаешь, что читаешь.\n"
        "!!! " + "=" * 68
    )


def lens_same_judge_warning(judge_model: str, settings: Settings, cases) -> str | None:
    """The same loud line for L2's lens cases and L3's garden cases, else
    None. They generate on `LLM_MODEL_SAFETY` (`_review_provider`,
    `lens_garden.build_garden_provider`), not `LLM_MODEL`, so
    `same_judge_warning` alone would let a judge set to that model grade
    its own lens rounds and gardens unnoticed."""
    lens_ids = [case.id for case in cases if case.input["kind"] in (LENS_REVIEW, LENS_GARDEN)]
    if not lens_ids or judge_model != settings.LLM_MODEL_SAFETY:
        return None
    return (
        "\n"
        "!!! " + "=" * 68 + "\n"
        f"!!! ПРЕДУПРЕЖДЕНИЕ: судья и модель разбора совпадают ({judge_model}).\n"
        f"!!! Кейсы линзы ({', '.join(lens_ids)}) она оценивает сама: их оценка\n"
        "!!! не независима. Задай LLM_MODEL_JUDGE другой моделью.\n"
        "!!! " + "=" * 68
    )


def _providers(settings: Settings):
    """Main model for the candidates, judge model for the rubric.

    One shared AsyncOpenAI client, as app/main.py does -- the harness
    makes 26 calls and opening two pools for them would be silly.
    """
    client = build_client(settings.OPENROUTER_API_KEY)
    # No web_search_max_results: milestone 4a removed both the provider
    # argument and the LLM_WEB_SEARCH_MAX_RESULTS setting, and these two
    # constructions kept passing it, so every real run died with an
    # AttributeError before its first call. tests/test_eval_providers.py
    # now builds them for real (no network) so that cannot recur.
    main = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL,
        max_tokens=settings.LLM_MAX_TOKENS,
        temperature=settings.LLM_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        client=client,
    )
    judge_model = judge_model_for(settings)
    judge = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=judge_model,
        max_tokens=settings.LLM_CHEAP_MAX_TOKENS,
        temperature=settings.LLM_CHEAP_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        client=client,
        structured_outputs=settings.LLM_STRUCTURED_OUTPUTS,
    )
    return main, judge, judge_model, client


def _review_provider(settings: Settings, client):
    """The weekly review's provider, as app/main.py builds its
    `safety_provider`: the lens round runs on whatever the review was
    handed, so a lens case has to run on the same model and settings."""
    return OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL_SAFETY,
        max_tokens=settings.LLM_SAFETY_MAX_TOKENS,
        temperature=settings.LLM_SAFETY_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        client=client,
        structured_outputs=settings.LLM_STRUCTURED_OUTPUTS,
    )


# app/core/lens_review.py's grounding schema name.
GROUNDING_SCHEMA_NAME = "anchor_lens_grounding"
# app/core/idle/lens_garden.py's schema name (L3).
GARDEN_SCHEMA_NAME = lens_garden.GARDEN_SCHEMA.name


def raw_grounding_text(reply: str) -> str:
    """Every raw grounded proposal's `text` and `reason`, one per line,
    before any validation; empty when the reply is not that JSON (parsed
    as app/core/lens_review.py parses it)."""
    payload = parse_json(reply)
    items = payload.get("proposals") if isinstance(payload, dict) else None
    lines = []
    for item in items if isinstance(items, list) else ():
        if isinstance(item, dict):
            lines.extend(
                value for value in (item.get("text"), item.get("reason")) if isinstance(value, str)
            )
    return "\n".join(lines)


def raw_garden_text(reply: str) -> str:
    """Every raw gap's `title` and `detail` and every cluster name, one
    per line, before `validate()`; empty when the reply is not that
    JSON. A gap `screen()` dropped still fails a forbidden pattern."""
    payload = parse_json(reply)
    if not isinstance(payload, dict):
        return ""
    lines = []
    for key, fields in (("gaps", ("title", "detail")), ("clusters", ("name",))):
        items = payload.get(key)
        for item in items if isinstance(items, list) else ():
            if isinstance(item, dict):
                lines.extend(
                    value for value in (item.get(field) for field in fields) if isinstance(value, str)
                )
    return "\n".join(lines)


class _Metered:
    """Wraps a provider for one lens case: adds up what its calls cost
    and keeps the first exception. app/core/lens_review.py swallows a
    provider error into a `fallback` round, as production must; the
    harness must not, since a case whose calls never ran has proved
    nothing ("a failed call is a failed case", as above)."""

    def __init__(self, provider) -> None:
        self._provider = provider
        self.usd_cost = 0.0
        self.error: str | None = None
        # The grounding call's reply as the model gave it, before
        # `validate_grounding` and its `screen()` drop anything: a case
        # checks it too, so a proposal the floor caught still fails.
        self.grounding_raw: str | None = None
        # L3: the garden call's reply as the model gave it, likewise.
        self.garden_raw: str | None = None

    async def complete(self, messages, *, conversation_id, json_schema=None):
        try:
            response = await self._provider.complete(
                messages, conversation_id=conversation_id, json_schema=json_schema
            )
        except Exception as exc:
            if self.error is None:
                self.error = f"{type(exc).__name__}: {str(exc)[:160]}"
            raise
        self.usd_cost += float(response.usage.cost_usd or 0.0)
        if json_schema is not None and json_schema.name == GROUNDING_SCHEMA_NAME:
            self.grounding_raw = response.text
        if json_schema is not None and json_schema.name == GARDEN_SCHEMA_NAME:
            self.garden_raw = response.text
        return response

    async def close(self) -> None:  # pragma: no cover - the run closes the client
        return None


async def run_lens_case(
    sessionmaker,
    case: Case,
    settings: Settings,
    clock,
    review,
    judge,
    *,
    amendments: list[str] | None = None,
) -> Outcome:
    """One `lens_review` case for real: seed, run the round, check, judge.

    The session stays open through the round -- `lens_review.apply`
    reads the catalog and writes the round as it goes, as it does inside
    `analyze_week`.
    """
    if review is None:
        return Outcome(
            case, "", [], judge_module.Verdict({}, [], False), 0.0,
            "нет провайдера разбора (LLM_MODEL_SAFETY) для кейса линзы",
        )
    metered = _Metered(review)
    try:
        async with sessionmaker() as session:
            await scenario.reset(session)
            state = await scenario.seed(session, case, clock, amendments=amendments)
            run = await scenario.run_lens_review(
                session, case, state, scenario.lens_settings(settings), clock, metered
            )
    except Exception as exc:  # noqa: BLE001 - a failed case, not a failed run
        return Outcome(
            case, "", [], judge_module.Verdict({}, [], False), metered.usd_cost,
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
    if metered.error is not None:
        return Outcome(
            case, "", [], judge_module.Verdict({}, [], False), metered.usd_cost, metered.error
        )

    reply = scenario.render_lens_run(run)
    check_results = checks_module.run_all(run.proposal_text, case.checks, settings)
    if case.checks.get("forbidden_regex") and metered.grounding_raw is not None:
        raw = checks_module.forbidden(
            raw_grounding_text(metered.grounding_raw), case.checks["forbidden_regex"]
        )
        check_results.append(
            checks_module.Result("forbidden_regex_raw", raw.passed, raw.detail)
        )
    check_results += checks_module.lens_checks(
        case.checks, outcome=run.outcome, selected=run.selected, proposals=run.proposals
    )
    verdict = await judge_module.judge(
        judge,
        items=case.judge_items,
        case_title=case.title,
        prompt_text=scenario.situation(case),
        reply=reply,
    )
    return Outcome(case, reply, check_results, verdict, metered.usd_cost + verdict.usd_cost)


async def run_garden_case(
    sessionmaker,
    case: Case,
    settings: Settings,
    clock,
    garden,
    judge,
    *,
    amendments: list[str] | None = None,
) -> Outcome:
    """One `lens_garden` case for real (L3): seed, step 1, the call,
    `validate()`, check, judge. A reply that does not parse fails the
    case, as it fails the idle run."""
    if garden is None:
        return Outcome(
            case, "", [], judge_module.Verdict({}, [], False), 0.0,
            "нет провайдера сада (LLM_MODEL_SAFETY) для кейса сада",
        )
    metered = _Metered(garden)
    try:
        async with sessionmaker() as session:
            await scenario.reset(session)
            await scenario.seed(session, case, clock, amendments=amendments)
            run = await scenario.run_lens_garden(session, clock, metered)
    except Exception as exc:  # noqa: BLE001 - a failed case, not a failed run
        return Outcome(
            case, "", [], judge_module.Verdict({}, [], False), metered.usd_cost,
            metered.error or f"{type(exc).__name__}: {str(exc)[:200]}",
        )

    reply = scenario.render_garden_run(run)
    check_results = checks_module.run_all(run.gap_text if run else "", case.checks, settings)
    if case.checks.get("forbidden_regex") and metered.garden_raw is not None:
        raw = checks_module.forbidden(
            raw_garden_text(metered.garden_raw), case.checks["forbidden_regex"]
        )
        check_results.append(checks_module.Result("forbidden_regex_raw", raw.passed, raw.detail))
    check_results += checks_module.garden_checks(case.checks, gaps=run.gaps if run else None)
    verdict = await judge_module.judge(
        judge,
        items=case.judge_items,
        case_title=case.title,
        prompt_text=scenario.situation(case),
        reply=reply,
    )
    return Outcome(case, reply, check_results, verdict, metered.usd_cost + verdict.usd_cost)


async def run_case(
    sessionmaker,
    case: Case,
    settings: Settings,
    clock,
    main,
    judge,
    dry_run: bool,
    *,
    amendments: list[str] | None = None,
    review=None,
    garden=None,
) -> Outcome:
    """Run one case against a fresh, seeded scenario.

    `amendments` (5d) overrides the case's own `setup.amendments` list
    when given -- eval/trial.py's `run_blocking_subset` passes the
    amendment_trial's own candidate-plus-every-other-active-amendment
    set here, so a trial exercises the persona with the amendment
    actually in place rather than whatever (if anything) a case file
    happens to seed on its own.

    `review` (L2) is the weekly review's provider, needed only by a
    `lens_review` case (`run_lens_case`); a dry run builds such a case's
    prompts through `scenario.build` like any other. eval/trial.py
    passes none: its subset is the blocking cases, and no lens case is
    blocking -- one that became blocking would fail there loudly rather
    than run on the persona model.

    `garden` (L3) is the lens garden's own provider, for a `lens_garden`
    case (`run_garden_case`), on the same terms: a dry run builds its
    messages through `scenario.build`, and eval/trial.py passes none.
    """
    if case.input["kind"] == LENS_REVIEW and not dry_run:
        return await run_lens_case(
            sessionmaker, case, settings, clock, review, judge, amendments=amendments
        )
    if case.input["kind"] == LENS_GARDEN and not dry_run:
        return await run_garden_case(
            sessionmaker, case, settings, clock, garden, judge, amendments=amendments
        )

    async with sessionmaker() as session:
        await scenario.reset(session)
        state = await scenario.seed(session, case, clock, amendments=amendments)
        messages = await scenario.build(session, case, state, settings, clock)

    if dry_run:
        rendered = "\n\n".join(f"[{m.role}] {m.content}" for m in messages)
        return Outcome(case, rendered, [], judge_module.Verdict({}, [], True), 0.0)

    try:
        response = await main.complete(messages, conversation_id=f"anchor-eval-{case.id}")
    except Exception as exc:  # noqa: BLE001 - a failed call is a failed case
        return Outcome(
            case, "", [], judge_module.Verdict({}, [], False), 0.0, str(exc)[:200]
        )

    reply = response.text.strip()
    check_results = checks_module.run_all(reply, case.checks, settings)
    verdict = await judge_module.judge(
        judge,
        items=case.judge_items,
        case_title=case.title,
        prompt_text=scenario.situation(case),
        reply=reply,
    )
    cost = float(response.usage.cost_usd or 0.0) + verdict.usd_cost
    return Outcome(case, reply, check_results, verdict, cost)


def render_report(
    outcomes: list[Outcome], settings: Settings, judge_model: str, started: datetime.datetime
) -> str:
    total = sum(o.usd_cost for o in outcomes)
    failed = [o for o in outcomes if not o.passed]
    blocking = [o for o in failed if o.case.blocking]

    lines = [
        f"# Eval — {started.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        f"- Модель: `{settings.LLM_MODEL}`",
        f"- Судья: `{judge_model}`",
        *(
            [f"- Модель разбора и сада (кейсы линзы): `{settings.LLM_MODEL_SAFETY}`"]
            if any(o.case.input["kind"] in (LENS_REVIEW, LENS_GARDEN) for o in outcomes)
            else []
        ),
        f"- Кейсов: {len(outcomes)} · прошло: {len(outcomes) - len(failed)} "
        f"· упало: {len(failed)} (блокирующих: {len(blocking)})",
        f"- Стоимость: ${total:.4f}",
        "",
        "| # | Кейс | Блок. | Итог | Проверки | Рубрика |",
        "|---|------|-------|------|----------|---------|",
    ]
    for outcome in outcomes:
        case = outcome.case
        checks_cell = (
            ", ".join(f"{r.name} {'✅' if r.passed else '❌'}" for r in outcome.check_results)
            or "—"
        )
        lines.append(
            f"| {case.id} | {case.title} | {'да' if case.blocking else '—'} | "
            f"{'✅' if outcome.passed else '❌'} | {checks_cell} | "
            f"{judge_module.render_scores(outcome.verdict)} |"
        )

    lines.extend(["", "## Ответы", ""])
    for outcome in outcomes:
        lines.append(f"### {outcome.case.id} — {outcome.case.title}")
        if outcome.error:
            lines.extend([f"**Ошибка:** {outcome.error}", ""])
            continue
        if outcome.failures:
            lines.append(f"**Не прошло:** {', '.join(outcome.failures)}")
        for result in outcome.check_results:
            lines.append(f"- {result.name}: {result.detail}")
        lines.extend(["", "```", outcome.reply, "```", ""])

    return "\n".join(lines) + "\n"


async def main_async(args) -> int:
    settings = get_settings()
    if not settings.OPENROUTER_API_KEY and not args.dry_run:
        raise SystemExit("OPENROUTER_API_KEY is not set; eval.run hits the real API.")

    cases = load_all()
    if args.case:
        cases = [c for c in cases if c.id.startswith(args.case)]
        if not cases:
            raise SystemExit(f"no case matching {args.case!r}")

    clock = SystemClock()
    started = datetime.datetime.now(datetime.timezone.utc)

    # Checked before a single call is made: the point is to stop the run,
    # not to annotate a report nobody will re-read.
    warning = same_judge_warning(judge_model_for(settings), settings)
    if warning is not None:
        print(warning, flush=True)
        blocking = [c for c in cases if c.blocking]
        if blocking and not args.allow_same_judge and not args.dry_run:
            print(
                f"\nОстановлено: {len(blocking)} блокирующих кейсов и несамостоятельный судья.",
                flush=True,
            )
            return EXIT_SAME_JUDGE
    lens_warning = lens_same_judge_warning(judge_model_for(settings), settings, cases)
    if lens_warning is not None:
        print(lens_warning, flush=True)

    # A dry run builds no provider: it exists precisely so the prompt
    # path can be exercised on a machine with no key and no budget.
    if args.dry_run:
        main = judge = review = garden = client = None
        judge_model = judge_model_for(settings)
    else:
        main, judge, judge_model, client = _providers(settings)
        review = _review_provider(settings, client)
        garden = lens_garden.build_garden_provider(settings, client)

    outcomes: list[Outcome] = []
    try:
        async with throwaway_sessionmaker() as sessionmaker:
            for case in cases:
                outcome = await run_case(
                    sessionmaker, case, settings, clock, main, judge, args.dry_run,
                    review=review, garden=garden,
                )
                outcomes.append(outcome)
                if args.dry_run:
                    print(f"·  {case.id} {case.title}", flush=True)
                    continue
                mark = "✅" if outcome.passed else "❌"
                print(f"{mark} {case.id} {case.title}", flush=True)
                if outcome.failures:
                    print(f"    {', '.join(outcome.failures)}", flush=True)
    finally:
        if client is not None:
            await client.close()

    if args.dry_run:
        for outcome in outcomes:
            print(f"\n===== {outcome.case.id} =====\n{outcome.reply}")
        return 0

    report = render_report(outcomes, settings, judge_model, started)
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"{started.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(report, encoding="utf-8")
    print(f"\nотчёт: {path}")
    print(f"стоимость: ${sum(o.usd_cost for o in outcomes):.4f}")

    failed = [o for o in outcomes if not o.passed]
    if any(o.case.blocking for o in failed):
        return 1
    return 2 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Anchor eval harness (manual, costs money)")
    parser.add_argument("--case", help="run only cases whose id starts with this")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build every prompt and print it; make no API calls",
    )
    parser.add_argument(
        "--allow-same-judge",
        action="store_true",
        help=(
            "run blocking cases even when the judge is the model under test. "
            "The scores are not independent; read them as 'nothing obviously "
            "broke', never as 'verified'."
        ),
    )
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
