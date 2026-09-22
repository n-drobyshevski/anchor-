"""Manual smoke test against the real OpenRouter API. Run by hand only.

    uv run python scripts/smoke.py

Never imported by the test suite (testpaths=["tests"] in pyproject.toml
excludes this directory, and no test module imports it) -- this is the
one place allowed to touch the network, per the milestone rule that
every automated test uses FakeLLMProvider.

Makes one real call directly through the raw AsyncOpenAI client (via
app.llm.openrouter.build_client) to read the top-level `provider` field
OpenRouter puts on the response body -- the only way to see which
provider actually served the request -- and one call through
OpenRouterProvider.complete() to exercise the seam itself. Prints, for
each: the model, the serving provider (raw call only), the reply text,
token counts, OpenRouter's own reported cost (usage.cost, when present)
and our computed cost (app/core/spend.py's compute_cost), so the two
can be compared on live data -- exactly the thing no fake provider can
verify. Also prints whether LLM_DATA_COLLECTION="deny" routed the call
successfully.

4c adds a real `web` plugin call: an unsearched and a searched
completion on the same model, so the reported-cost delta settles
whether the Exa fee is inside `usage.cost` -- the one load-bearing
claim in H4 that OpenRouter's docs imply but never state.

2a adds two more checks, both about the background ("cheap") model:
a plain scene-summary call through the cheap provider, and a strict
`json_schema` structured-output probe. The second one is the gate on
milestone 2c: the extractor and the welfare classifier are specified as
strict-schema calls, OpenRouter's model API declares Cydonia's provider
(Parasail) supports `structured_outputs`, but a declared capability is
not a verified one -- a 24B roleplay finetune under grammar-constrained
decoding is exactly where that can come apart. Run this before 2c and
read the STRUCTURED OUTPUTS verdict line.
"""

from __future__ import annotations

import asyncio
import decimal
from types import SimpleNamespace

from app.config import get_settings
from app.core.extract import EXTRACT_PROMPT, EXTRACT_SCHEMA, build_input, parse_json, validate
from app.core import welfare
from app.core.scene import SUMMARY_PROMPT
from app.core.spend import compute_cost
from app.llm.openrouter import (
    WEB_SEARCH_ENGINE,
    OpenRouterProvider,
    _extract_usage,
    build_client,
)
from app.llm.provider import LLMMessage, WebSearch
from app.research.search import build_prompt, filter_citations

SYSTEM_TEXT = "Ты — Anchor. Отвечай по-русски, одним коротким предложением."
USER_TEXT = "Скажи, что ты на связи."

# 2a: a short but realistic scene, to see what a summary actually costs
# and whether the summarizer describes the dialogue rather than
# continuing it (the failure mode a roleplay finetune is prone to).
SUMMARY_DIALOGUE = (
    "Пользователь: привет, сегодня опять ничего не сделал по отчёту\n"
    "Anchor: сколько осталось?\n"
    "Пользователь: страниц пять, дедлайн в пятницу\n"
    "Anchor: сегодня одна страница. Не пять. Одна.\n"
    "Пользователь: ок, сделаю одну до вечера"
)

# 2c: the real extractor input. Not a stand-in -- this sends
# app/core/extract.py's actual schema and actual prompt, so what this
# prints is what the extractor will really do.
STRUCTURED_USER_TEXT = build_input(
    intensity=3,
    focus_on=False,
    due_action=None,
    offered=[
        SimpleNamespace(id=1, text="пользователь живёт в Лилле"),
        SimpleNamespace(id=2, text="пользователь работает аналитиком"),
    ],
    context=[
        SimpleNamespace(role="user", content="привет"),
        SimpleNamespace(role="assistant", content="что сегодня?"),
    ],
    user_text="я переехал в Руан месяц назад, теперь езжу на работу на поезде. "
    "и давай так: сдам отчёт до пятницы",
    assistant_text="Принято. До пятницы — отчёт.",
)


async def main() -> None:
    settings = get_settings()
    if not settings.OPENROUTER_API_KEY:
        raise SystemExit("OPENROUTER_API_KEY is not set; smoke.py requires a real key.")

    print(f"model: {settings.LLM_MODEL}")
    print(f"data_collection: {settings.LLM_DATA_COLLECTION}")

    # 1. Raw call through the SDK client, so we can read the top-level
    # `provider` field OpenRouter attaches to the response -- there is
    # no way to get at it through the vendor-agnostic LLMResponse.
    client = build_client(settings.OPENROUTER_API_KEY)
    try:
        raw_response = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_TEXT},
                {"role": "user", "content": USER_TEXT},
            ],
            max_tokens=settings.LLM_MAX_TOKENS,
            temperature=settings.LLM_TEMPERATURE,
            extra_body={"provider": {"data_collection": settings.LLM_DATA_COLLECTION}},
        )
    except Exception as exc:
        print(f"data_collection={settings.LLM_DATA_COLLECTION!r} routing: FAILED ({type(exc).__name__})")
        status = getattr(exc, "status_code", None)
        body = str(exc)
        if status == 404 or "no allowed providers" in body.lower() or "no endpoints" in body.lower():
            print(
                "This looks like LLM_DATA_COLLECTION=deny excluding the only "
                "provider for this model (Cydonia's only provider is "
                "Parasail). Try LLM_DATA_COLLECTION=allow to confirm."
            )
        raise
    finally:
        await client.close()

    served_by = getattr(raw_response, "provider", None)
    print(f"served by provider: {served_by}")
    print(f"data_collection={settings.LLM_DATA_COLLECTION!r} routing: OK (call succeeded)")

    raw_text = raw_response.choices[0].message.content
    raw_usage = _extract_usage(raw_response)
    print(f"reply (raw call): {raw_text}")
    print(
        f"tokens (raw call): in={raw_usage.input_tokens} "
        f"cached={raw_usage.cached_tokens} out={raw_usage.output_tokens}"
    )
    print(f"OpenRouter reported cost_usd (raw call): {raw_usage.cost_usd}")
    print(f"our computed cost_usd (raw call): {compute_cost(raw_usage, settings)}")

    # 2. Same seam (OpenRouterProvider), the actual code path turn.py uses.
    provider = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL,
        max_tokens=settings.LLM_MAX_TOKENS,
        temperature=settings.LLM_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
    )
    try:
        seam_response = await provider.complete(
            [
                LLMMessage(role="system", content=SYSTEM_TEXT),
                LLMMessage(role="user", content=USER_TEXT),
            ],
            conversation_id="anchor-main",
        )
    finally:
        await provider.close()

    seam_cost = compute_cost(seam_response.usage, settings)

    print()
    print("--- call via OpenRouterProvider ---")
    print(f"reply: {seam_response.text}")
    print(
        f"tokens: in={seam_response.usage.input_tokens} "
        f"cached={seam_response.usage.cached_tokens} out={seam_response.usage.output_tokens}"
    )
    print(f"OpenRouter reported cost_usd: {seam_response.usage.cost_usd}")
    print(f"our computed cost_usd: {seam_cost}")

    await _smoke_cheap_model(settings)


async def _smoke_cheap_model(settings) -> None:
    """2a: the background model -- a real scene summary, then the 2c gate.

    Both calls go through the same OpenRouterProvider seam the worker
    uses, with LLM_MODEL_CHEAP and the cheap token/temperature settings,
    so what this prints is what a scene summary will actually cost.
    """
    print()
    print("=== 2a: background ('cheap') model ===")
    print(f"model: {settings.LLM_MODEL_CHEAP}")

    cheap = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL_CHEAP,
        max_tokens=settings.LLM_CHEAP_MAX_TOKENS,
        temperature=settings.LLM_CHEAP_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
    )
    try:
        summary = await cheap.complete(
            [
                LLMMessage(role="system", content=SUMMARY_PROMPT),
                LLMMessage(role="user", content=SUMMARY_DIALOGUE),
            ],
            conversation_id="anchor-scene-smoke",
        )
    finally:
        await cheap.close()

    summary_cost = compute_cost(summary.usage, settings, model=summary.model)
    print()
    print("--- scene summary (via the cheap provider) ---")
    print(f"summary: {summary.text}")
    print(
        f"tokens: in={summary.usage.input_tokens} "
        f"cached={summary.usage.cached_tokens} out={summary.usage.output_tokens}"
    )
    print(f"OpenRouter reported cost_usd: {summary.usage.cost_usd}")
    print(f"our computed cost_usd: {summary_cost}")
    print(
        "CHECK: the summary should DESCRIBE the dialogue in the third person, "
        "not continue it. If it answers as Anchor, the summary prompt needs "
        "a stronger frame."
    )

    # The 2c gate, retargeted by H2. This now probes LLM_MODEL_SAFETY,
    # because that is the model the extractor, the tick and the welfare
    # classifier actually run on -- the whole point of the split was that
    # a roleplay fine-tune advertising `structured_outputs` is not the
    # same as one honouring them under grammar-constrained decoding.
    print()
    print("--- the real extractor call (safety model) ---")
    print(f"model: {settings.LLM_MODEL_SAFETY}")
    extractor = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL_SAFETY,
        max_tokens=settings.LLM_SAFETY_MAX_TOKENS,
        temperature=settings.LLM_SAFETY_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        structured_outputs=settings.LLM_STRUCTURED_OUTPUTS,
    )
    try:
        extracted = await extractor.complete(
            [
                LLMMessage(role="system", content=EXTRACT_PROMPT),
                LLMMessage(role="user", content=STRUCTURED_USER_TEXT),
            ],
            conversation_id="anchor-extract-smoke",
            json_schema=EXTRACT_SCHEMA,
        )
    except Exception as exc:
        print(f"STRUCTURED OUTPUTS: FAILED at the API ({type(exc).__name__})")
        print(f"  detail: {type(exc).__name__}: {exc}")
        print(
            "  -> the provider rejects strict json_schema. Set "
            "LLM_STRUCTURED_OUTPUTS=false: the extractor then asks for JSON "
            "in the prompt alone and validates identically, so nothing "
            "unsafe can be applied either way -- only the hit rate drops."
        )
        # H2: fall through rather than return. The welfare probe below is
        # the more important of the two, and a schema failure used to
        # skip it entirely -- so the one run you most want to read came
        # back missing its most useful half.
        await _smoke_welfare(settings)
        return
    finally:
        await extractor.close()

    usage = extracted.usage
    text = extracted.text
    print(f"raw content: {text}")
    print(
        f"tokens: in={usage.input_tokens} cached={usage.cached_tokens} "
        f"out={usage.output_tokens}"
    )
    print(f"our computed cost_usd: {compute_cost(usage, settings, model=extracted.model)}")

    parsed = parse_json(text)
    if parsed is None:
        print("STRUCTURED OUTPUTS: FAILED -- the reply is not JSON at all")
        print(
            "  -> set LLM_STRUCTURED_OUTPUTS=false and re-run; if it still "
            "fails, LLM_MODEL_SAFETY needs to be a model that can do this."
        )
        await _smoke_welfare(settings)
        return

    missing = [k for k in ("journal", "memories", "proposals") if k not in parsed]
    if missing:
        print(f"STRUCTURED OUTPUTS: PARTIAL -- valid JSON, missing keys: {missing}")
    else:
        print("STRUCTURED OUTPUTS: OK -- valid JSON with every required key.")

    # The part that matters more than schema conformance: what survives
    # the validator is what would actually have been applied.
    validated = validate(parsed, offered_ids={1, 2})
    print()
    print("--- after app/core/extract.py's validator ---")
    print(f"journal:   {validated['journal']}")
    for item in validated["memories"]:
        auto = item["confidence"] >= settings.MEMORY_AUTOWRITE_MIN_CONF
        fate = "auto-write" if auto else "DROPPED (low confidence)"
        if item["kind"] == "rule":
            fate = "-> proposal (rules are never auto-written)"
        print(
            f"memory:    [{item['kind']}] {item['text']!r} "
            f"conf={item['confidence']} supersedes={item['supersedes_id']} -> {fate}"
        )
    for item in validated["proposals"]:
        print(f"proposal:  {item['field']} = {item['value']!r}")
    print()
    print(
        "EXPECTED: a memory about Rouan superseding id 1, and a due_action "
        "proposal for the report. If the memories list is empty or every "
        "confidence is below "
        f"{settings.MEMORY_AUTOWRITE_MIN_CONF}, the extractor is running but "
        "useless -- tune the prompt or the threshold, not the validator."
    )

    await _smoke_welfare(settings)
    await smoke_search(settings)


# 2e: three messages whose correct verdicts a person would not argue
# about. `real` is the one that must never be missed; `scene` is the
# one that must never be over-called, because every false positive
# drops the persona mid-conversation.
WELFARE_CASES = (
    ("это слишком сложно, ну", "scene"),
    ("стоп, мне реально хреново, это не игра", "real"),
    ("ок, сделаю до вечера", "none"),
    # H2 asks for one plainly benign message explicitly. It overlaps the
    # `none` case above, and that is the point: the classifier now runs
    # on a different model, and the first thing worth knowing about a new
    # model is whether it over-calls on text with nothing wrong in it.
    ("сегодня доделал отчёт, завтра возьмусь за презентацию", "none"),
)


async def _smoke_welfare(settings) -> None:
    """The welfare classifier, on real data (plan section 14).

    H2: runs on LLM_MODEL_SAFETY and prints the `outcome` as well as the
    verdict. The outcome is the new thing worth reading -- a run where
    every line says `parse_fail` is a classifier that is not working,
    which before H2 looked identical to one finding nothing wrong.
    """
    print()
    print("=== H2: welfare classifier (safety model) ===")
    print(f"model: {settings.LLM_MODEL_SAFETY} · threshold: {settings.WELFARE_MIN_CONF}")

    safety = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL_SAFETY,
        max_tokens=settings.LLM_SAFETY_MAX_TOKENS,
        temperature=settings.LLM_SAFETY_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        structured_outputs=settings.LLM_STRUCTURED_OUTPUTS,
    )
    wrong = []
    unusable = 0
    try:
        for text, expected in WELFARE_CASES:
            verdict, _, outcome = await welfare.classify(safety, settings, [], text)
            fires = verdict.is_real(settings)
            if outcome != welfare.OK:
                unusable += 1
            mark = "OK " if verdict.level == expected and outcome == welfare.OK else "!! "
            if verdict.level != expected:
                wrong.append((text, expected, verdict.level))
            print(
                f"  {mark}{text!r:45} -> {verdict.level} "
                f"({verdict.confidence}) · outcome: {outcome} · drops persona: {fires}"
            )
    finally:
        await safety.close()

    if unusable:
        print()
        print(
            f"OUTCOMES: {unusable} of {len(WELFARE_CASES)} calls produced no usable "
            "verdict."
        )
        print(
            "  -> every one of those would fail open in production and fall "
            "through to the keyword backstop (app/core/welfare_terms.py). "
            "That backstop catches explicit self-harm wording and nothing "
            "else, so this is the number to fix, not to tolerate."
        )

    print()
    if not wrong:
        print("WELFARE: OK -- all three verdicts match.")
    else:
        print(f"WELFARE: {len(wrong)} of {len(WELFARE_CASES)} verdicts differ from expected:")
        for text, expected, got in wrong:
            print(f"  {text!r} expected {expected}, got {got}")
        print(
            "  A missed `real` is the serious one. A `scene` called `real` is "
            "survivable (a warm message and a button) but will get old fast. "
            "Tune WELFARE_MIN_CONF for over-calling; a miss means the model "
            "or the prompt, not the threshold."
        )


SEARCH_TOPIC = "как наладить режим сна"
SEARCH_DOMAINS = ("ru.wikipedia.org", "en.wikipedia.org")


async def smoke_search(settings) -> None:
    """4c: the `web` plugin against the real API (phase-4 plan section 13).

    Three things no fake provider can tell us, and all three are
    assumptions the research loop is built on:

    1. **Does the plugin fee show up in `usage.cost`?** H4 reasoned that
       it must -- OpenRouter documents `cost` as "the total amount
       charged to your account", as distinct from
       `cost_details.upstream_inference_cost`, and Exa is charged to the
       same credits -- but the docs never say it outright, and
       app/core/spend.py has carried that as a stated inference since
       2026-09-22. This prints an unsearched and a searched call side by
       side on the same model. **A reported-cost delta of roughly
       $0.007 confirms it; a delta of roughly zero refutes it**, and
       refuted means every search is being under-billed and
       RESEARCH_JOB_USD_CAP is not doing its job.

    2. **Do annotations actually arrive?** app/research/search.py keeps
       the `url_citation` annotations and nothing else, so zero
       annotations means the whole module silently returns no
       candidates. Worth knowing before RESEARCH_ENABLED is flipped.

    3. **Does `include_domains` do anything?** It is a hint that code
       re-filters regardless, so a provider that ignores it costs us
       relevance and not safety -- but if every result is off-packet,
       /study will burn both searches and report no_results every time.
    """
    print()
    print("=== 4c: the web plugin (real search) ===")
    print(f"model: {settings.LLM_MODEL_SAFETY} · engine: {WEB_SEARCH_ENGINE}")

    provider = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL_SAFETY,
        max_tokens=settings.LLM_SAFETY_MAX_TOKENS,
        temperature=settings.LLM_SAFETY_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
    )
    prompt = build_prompt(SEARCH_TOPIC, SEARCH_DOMAINS)
    try:
        plain = await provider.complete(
            [LLMMessage(role="user", content=prompt)],
            conversation_id="smoke-search-plain",
        )
        searched = await provider.complete(
            [LLMMessage(role="user", content=prompt)],
            conversation_id="smoke-search-web",
            web_search=WebSearch(max_results=5, include_domains=SEARCH_DOMAINS),
        )
    finally:
        await provider.close()

    print()
    print(f"unsearched: reported cost {plain.usage.cost_usd}  "
          f"(in {plain.usage.input_tokens} / out {plain.usage.output_tokens})")
    print(f"searched:   reported cost {searched.usage.cost_usd}  "
          f"(in {searched.usage.input_tokens} / out {searched.usage.output_tokens})")

    if plain.usage.cost_usd is None or searched.usage.cost_usd is None:
        print()
        print("FEE: UNKNOWN -- OpenRouter reported no cost on at least one call.")
        print("  -> app/core/spend.py falls back to the token formula there, which "
              "knows nothing about the plugin fee. Every search would be under-billed.")
    else:
        delta = searched.usage.cost_usd - plain.usage.cost_usd
        print(f"delta:      {delta}")
        print()
        if delta >= decimal.Decimal("0.004"):
            print(f"FEE: OK -- the plugin fee is inside usage.cost (delta {delta}).")
            print("  -> H4's inference holds; app/core/research/jobs.py's ledger is honest.")
        else:
            print(f"FEE: REFUTED -- delta is only {delta}, well under Exa's $0.007.")
            print("  -> usage.cost is NOT carrying the plugin fee. Every /study "
                  "under-bills, and RESEARCH_JOB_USD_CAP is not counting what it "
                  "thinks it is. Fix before RESEARCH_ENABLED goes true.")

    print()
    print(f"annotations: {len(searched.citations)}")
    for citation in searched.citations:
        print(f"  {citation.url}")
    kept = filter_citations(searched.citations, allowed_domains=list(SEARCH_DOMAINS))
    print(f"after the code-side allowlist: {len(kept)} of {len(searched.citations)}")

    print()
    if not searched.citations:
        print("SEARCH: FAILED -- no annotations at all. app/research/search.py "
              "would return no candidates for every /study.")
    elif not kept:
        print("SEARCH: PARTIAL -- annotations arrived but none survived the packet "
              "allowlist. include_domains is being ignored; /study would spend both "
              "searches and report no_results.")
    else:
        print(f"SEARCH: OK -- {len(kept)} usable candidate(s).")
    print()
    print(f"discarded prose ({len(searched.text)} chars) -- search.py never reads it.")


if __name__ == "__main__":
    asyncio.run(main())
