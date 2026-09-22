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
from app.llm.openrouter import OpenRouterProvider, build_client
from eval import checks as checks_module
from eval import judge as judge_module
from eval import scenario
from eval.cases import Case, load_all
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


def _providers(settings: Settings):
    """Main model for the candidates, judge model for the rubric.

    One shared AsyncOpenAI client, as app/main.py does -- the harness
    makes 26 calls and opening two pools for them would be silly.
    """
    client = build_client(settings.OPENROUTER_API_KEY)
    # web_search_max_results is a required positional argument of
    # OpenRouterProvider. H5: both constructions here omitted it, so every
    # non-dry-run invocation of this harness died with a TypeError before
    # reaching the first API call -- which is the actual reason
    # eval/reports/ was still empty, rather than the missing key everyone
    # assumed. The value is irrelevant (the harness never searches) but
    # the argument is not optional.
    main = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL,
        max_tokens=settings.LLM_MAX_TOKENS,
        temperature=settings.LLM_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        web_search_max_results=settings.LLM_WEB_SEARCH_MAX_RESULTS,
        client=client,
    )
    judge_model = judge_model_for(settings)
    judge = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=judge_model,
        max_tokens=settings.LLM_CHEAP_MAX_TOKENS,
        temperature=settings.LLM_CHEAP_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        web_search_max_results=settings.LLM_WEB_SEARCH_MAX_RESULTS,
        client=client,
        structured_outputs=settings.LLM_STRUCTURED_OUTPUTS,
    )
    return main, judge, judge_model, client


async def run_case(
    sessionmaker, case: Case, settings: Settings, clock, main, judge, dry_run: bool
) -> Outcome:
    async with sessionmaker() as session:
        await scenario.reset(session)
        state = await scenario.seed(session, case, clock)
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

    # A dry run builds no provider: it exists precisely so the prompt
    # path can be exercised on a machine with no key and no budget.
    if args.dry_run:
        main = judge = client = None
        judge_model = judge_model_for(settings)
    else:
        main, judge, judge_model, client = _providers(settings)

    outcomes: list[Outcome] = []
    try:
        async with throwaway_sessionmaker() as sessionmaker:
            for case in cases:
                outcome = await run_case(
                    sessionmaker, case, settings, clock, main, judge, args.dry_run
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
