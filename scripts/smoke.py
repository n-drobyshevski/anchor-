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
from decimal import Decimal

from app.config import get_settings
from app.core.spend import compute_cost
from app.llm.openrouter import OpenRouterProvider, _extract_usage, build_client
from app.llm.provider import LLMMessage

SYSTEM_TEXT = "Ты — Anchor. Отвечай по-русски, одним коротким предложением."
USER_TEXT = "Скажи, что ты на связи."
# Needs current data, not something the model could plausibly know from
# training -- otherwise it might not trigger a real Exa lookup.
SEARCH_SYSTEM_TEXT = "Отвечай по-русски, коротко, одним-двумя предложениями."
SEARCH_USER_TEXT = "Какая сегодня дата и какая сейчас самая обсуждаемая новость в мире?"


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


if __name__ == "__main__":
    asyncio.run(main())
