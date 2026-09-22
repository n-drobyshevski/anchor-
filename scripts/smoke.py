"""Manual smoke test against the real OpenRouter API. Run by hand only.

    uv run python scripts/smoke.py

Never imported by the test suite (testpaths=["tests"] in pyproject.toml
excludes this directory, and no test module imports it) -- this is the
one place allowed to touch the network, per the milestone rule that
every automated test uses FakeLLMProvider.

Makes one real call directly through the raw AsyncOpenAI client (via
app.llm.openrouter.build_client) to read the top-level `provider` field
OpenRouter puts on the response body -- the only way to see which
provider actually served the request -- and two calls through
OpenRouterProvider.complete() to exercise the seam itself: one plain,
one with web_search=True. Prints, for each: the model, the serving
provider (raw call only), the reply text, token counts, OpenRouter's
own reported cost (usage.cost, when present) and our computed cost
(app/core/spend.py's compute_cost), so the two can be compared on live
data -- exactly the thing no fake provider can verify. Also prints
whether LLM_DATA_COLLECTION="deny" routed the call successfully.

2a adds two more checks, both about the background ("cheap") model:
a plain scene-summary call through the cheap provider, and a strict
`json_schema` structured-output probe. The second one is the gate on
milestone 2c: the extractor and the welfare classifier are specified as
strict-schema calls, OpenRouter's model API declares Cydonia's provider
(Parasail) supports `structured_outputs`, but a declared capability is
not a verified one -- a 24B roleplay finetune under grammar-constrained
decoding is exactly where that can come apart. Run this before 2c and
read the STRUCTURED OUTPUTS verdict line.

1f: the two OpenRouterProvider calls (unsearched vs. searched) exist
specifically to answer the open question in app/config.py's
LLM_WEB_SEARCH_PRICE_USD comment -- whether OpenRouter's reported
usage.cost already folds in Exa's $0.007/request fee. The script
prints the delta between the two reported costs and an explicit
interpretation line; read that line to decide whether
LLM_WEB_SEARCH_PRICE_USD should stay 0.0 or become 0.007.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from app.config import get_settings
from app.core.scene import SUMMARY_PROMPT
from app.core.spend import compute_cost
from app.llm.openrouter import OpenRouterProvider, _extract_usage, build_client
from app.llm.provider import LLMMessage

SYSTEM_TEXT = "Ты — Anchor. Отвечай по-русски, одним коротким предложением."
USER_TEXT = "Скажи, что ты на связи."
# Needs current data, not something the model could plausibly know from
# training -- otherwise it might not trigger a real Exa lookup.
SEARCH_SYSTEM_TEXT = "Отвечай по-русски, коротко, одним-двумя предложениями."
SEARCH_USER_TEXT = "Какая сегодня дата и какая сейчас самая обсуждаемая новость в мире?"

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

# 2a: the 2c gate. Deliberately the *shape* of the real extractor
# schema from plan section 8 -- nested objects, an enum, a nullable
# integer, bounded arrays -- not a toy {"answer": "string"}, because the
# thing that breaks under constrained decoding is nesting and
# nullability, not flat strings.
EXTRACTOR_SCHEMA = {
    "name": "anchor_extract",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["journal", "memories", "proposals"],
        "properties": {
            "journal": {"type": ["string", "null"]},
            "memories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "text", "supersedes_id", "confidence"],
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["identity", "preference", "event", "rule"],
                        },
                        "text": {"type": "string"},
                        "supersedes_id": {"type": ["integer", "null"]},
                        "confidence": {"type": "number"},
                    },
                },
            },
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field", "value", "reason"],
                    "properties": {
                        "field": {"type": "string", "enum": ["due_action", "focus_on"]},
                        "value": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    },
}

STRUCTURED_SYSTEM_TEXT = (
    "Ты — модуль учёта. По последнему обмену репликами верни JSON по схеме. "
    "Если ничего нового — пустые массивы и null."
)
STRUCTURED_USER_TEXT = (
    "Пользователь: я переехал в Руан месяц назад, теперь езжу на работу на поезде\n"
    "Anchor: и как, лучше?\n"
    "Пользователь: да, спокойнее. договорились: сдам отчёт до пятницы"
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

    # 2 & 3. Same seam (OpenRouterProvider), the actual code path
    # turn.py uses -- once without web_search, once with, so the two
    # reported costs can be diffed to see whether OpenRouter's
    # usage.cost already includes Exa's per-request fee.
    provider = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL,
        max_tokens=settings.LLM_MAX_TOKENS,
        temperature=settings.LLM_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
        web_search_max_results=settings.LLM_WEB_SEARCH_MAX_RESULTS,
    )
    try:
        unsearched = await provider.complete(
            [
                LLMMessage(role="system", content=SYSTEM_TEXT),
                LLMMessage(role="user", content=USER_TEXT),
            ],
            conversation_id="anchor-main",
        )
        searched = await provider.complete(
            [
                LLMMessage(role="system", content=SEARCH_SYSTEM_TEXT),
                LLMMessage(role="user", content=SEARCH_USER_TEXT),
            ],
            conversation_id="anchor-main",
            web_search=True,
        )
    finally:
        await provider.close()

    unsearched_cost = compute_cost(unsearched.usage, settings)
    searched_cost = compute_cost(searched.usage, settings)

    print()
    print("--- unsearched call (via OpenRouterProvider) ---")
    print(f"reply: {unsearched.text}")
    print(
        f"tokens: in={unsearched.usage.input_tokens} "
        f"cached={unsearched.usage.cached_tokens} out={unsearched.usage.output_tokens}"
    )
    print(f"OpenRouter reported cost_usd: {unsearched.usage.cost_usd}")
    print(f"our computed cost_usd: {unsearched_cost}")

    print()
    print("--- searched call, web_search=True (via OpenRouterProvider) ---")
    print(f"reply: {searched.text}")
    print(
        f"tokens: in={searched.usage.input_tokens} "
        f"cached={searched.usage.cached_tokens} out={searched.usage.output_tokens}"
    )
    print(f"web_search_requests: {searched.usage.web_search_requests}")
    print(f"OpenRouter reported cost_usd: {searched.usage.cost_usd}")
    print(f"our computed cost_usd: {searched_cost}")

    print()
    if unsearched.usage.cost_usd is not None and searched.usage.cost_usd is not None:
        delta = searched.usage.cost_usd - unsearched.usage.cost_usd
        print(f"delta between reported cost_usd (searched - unsearched): {delta}")
        # Exa `auto` is $0.007/request. Token counts differ a little
        # between the two calls (different prompts/replies), so treat
        # anything reasonably close to 0.007 as "the fee is separate".
        if delta >= Decimal("0.005"):
            print(
                "delta ~= 0.007 -> OpenRouter EXCLUDES the Exa fee from "
                "usage.cost; set LLM_WEB_SEARCH_PRICE_USD=0.007"
            )
        else:
            print(
                "delta is well under 0.007 -> OpenRouter's reported cost_usd "
                "already covers the search fee; leave LLM_WEB_SEARCH_PRICE_USD=0.0"
            )
    else:
        print(
            "one of the two calls returned no cost_usd -- cannot compute a "
            "reported-cost delta; re-run once both calls report a cost."
        )

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
        web_search_max_results=settings.LLM_WEB_SEARCH_MAX_RESULTS,
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

    # The 2c gate. Raw client, not the provider seam: response_format is
    # not part of LLMProvider.complete() yet -- 2c adds it -- and this
    # probe exists precisely to decide whether it is safe to.
    print()
    print("--- strict json_schema probe (the 2c gate) ---")
    client = build_client(settings.OPENROUTER_API_KEY)
    try:
        response = await client.chat.completions.create(
            model=settings.LLM_MODEL_CHEAP,
            messages=[
                {"role": "system", "content": STRUCTURED_SYSTEM_TEXT},
                {"role": "user", "content": STRUCTURED_USER_TEXT},
            ],
            max_tokens=settings.LLM_CHEAP_MAX_TOKENS,
            temperature=settings.LLM_CHEAP_TEMPERATURE,
            response_format={"type": "json_schema", "json_schema": EXTRACTOR_SCHEMA},
            extra_body={
                "provider": {
                    "data_collection": settings.LLM_DATA_COLLECTION,
                    # Refuse to silently fall back to a provider that
                    # would ignore response_format and hand back prose.
                    "require_parameters": True,
                }
            },
        )
    except Exception as exc:
        print(f"STRUCTURED OUTPUTS: FAILED at the API ({type(exc).__name__})")
        print(f"  detail: {type(exc).__name__}: {exc}")
        print(
            "  -> 2c cannot use strict json_schema on this model. Fallback: "
            "json_object mode plus code-side validation, or a different "
            "LLM_MODEL_CHEAP. Report this before starting 2c."
        )
        return
    finally:
        await client.close()

    usage = _extract_usage(response)
    text = response.choices[0].message.content
    print(f"raw content: {text}")
    print(
        f"tokens: in={usage.input_tokens} cached={usage.cached_tokens} "
        f"out={usage.output_tokens}"
    )
    print(f"our computed cost_usd: {compute_cost(usage, settings, model=settings.LLM_MODEL_CHEAP)}")

    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        print(f"STRUCTURED OUTPUTS: FAILED -- not valid JSON ({type(exc).__name__})")
        print("  -> see the fallback note above. Report this before starting 2c.")
        return

    missing = [k for k in ("journal", "memories", "proposals") if k not in parsed]
    if missing:
        print(f"STRUCTURED OUTPUTS: PARTIAL -- valid JSON but missing keys: {missing}")
        print("  -> strict mode is not being enforced. Report this before starting 2c.")
        return

    if not isinstance(parsed["memories"], list) or not isinstance(parsed["proposals"], list):
        print("STRUCTURED OUTPUTS: PARTIAL -- memories/proposals are not arrays")
        print("  -> strict mode is not being enforced. Report this before starting 2c.")
        return

    print("STRUCTURED OUTPUTS: OK -- valid JSON, all required keys, correct types.")
    print(
        "  Sanity-check the CONTENT too: 'переехал в Руан' should appear as a "
        "memory, and 'сдать отчёт до пятницы' as a due_action proposal. "
        "Schema conformance without useful content still blocks 2c."
    )


if __name__ == "__main__":
    asyncio.run(main())
