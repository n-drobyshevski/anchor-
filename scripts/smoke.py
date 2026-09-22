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
token counts, OpenRouter's own reported cost (usage.cost, when
present) and our computed cost (app/core/spend.py's compute_cost), so
the two can be compared on live data -- exactly the thing no fake
provider can verify. Also prints whether LLM_DATA_COLLECTION="deny"
routed the call successfully.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.core.spend import compute_cost
from app.llm.openrouter import OpenRouterProvider, _extract_usage, build_client
from app.llm.provider import LLMMessage

SYSTEM_TEXT = "Ты — Anchor. Отвечай по-русски, одним коротким предложением."
USER_TEXT = "Скажи, что ты на связи."


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

    # 2. Same call again, this time through the seam (OpenRouterProvider),
    # to exercise the actual code path turn.py uses.
    provider = OpenRouterProvider(
        api_key=settings.OPENROUTER_API_KEY,
        model=settings.LLM_MODEL,
        max_tokens=settings.LLM_MAX_TOKENS,
        temperature=settings.LLM_TEMPERATURE,
        data_collection=settings.LLM_DATA_COLLECTION,
    )
    try:
        response = await provider.complete(
            [
                LLMMessage(role="system", content=SYSTEM_TEXT),
                LLMMessage(role="user", content=USER_TEXT),
            ],
            conversation_id="anchor-main",
        )
    finally:
        await provider.close()

    computed_cost = compute_cost(response.usage, settings)

    print(f"reply (via OpenRouterProvider): {response.text}")
    print(
        f"tokens (via OpenRouterProvider): in={response.usage.input_tokens} "
        f"cached={response.usage.cached_tokens} out={response.usage.output_tokens}"
    )
    print(f"OpenRouter reported cost_usd (via OpenRouterProvider): {response.usage.cost_usd}")
    print(f"our computed cost_usd (via OpenRouterProvider): {computed_cost}")


if __name__ == "__main__":
    asyncio.run(main())
