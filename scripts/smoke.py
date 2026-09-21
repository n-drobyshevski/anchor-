"""Manual smoke test against the real xAI API. Run by hand only.

    uv run python scripts/smoke.py

Never imported by the test suite (testpaths=["tests"] in pyproject.toml
excludes this directory, and no test module imports it) -- this is the
one place allowed to touch the network, per the milestone rule that
every automated test uses FakeLLMProvider.

Prints the reply text, token counts, and both xAI's own reported cost
(usage.cost_in_nano_usd, when present) and our computed cost (plan
section 10's formula), so the two can be compared on live data --
exactly the thing no fake provider can verify.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.core.spend import compute_cost
from app.llm.provider import LLMMessage
from app.llm.xai import XAIProvider


async def main() -> None:
    settings = get_settings()
    if not settings.XAI_API_KEY:
        raise SystemExit("XAI_API_KEY is not set; smoke.py requires a real key.")

    provider = XAIProvider(
        api_key=settings.XAI_API_KEY,
        model=settings.XAI_MODEL,
        reasoning_effort=settings.XAI_REASONING_EFFORT,
    )
    try:
        response = await provider.complete(
            [
                LLMMessage(role="system", content="Ты — Anchor. Отвечай по-русски, одним коротким предложением."),
                LLMMessage(role="user", content="Скажи, что ты на связи."),
            ],
            conversation_id="anchor-main",
        )
    finally:
        await provider.close()

    computed_cost = compute_cost(response.usage, settings)

    print(f"model: {response.model}")
    print(f"reply: {response.text}")
    print(
        f"tokens: in={response.usage.input_tokens} "
        f"cached={response.usage.cached_tokens} out={response.usage.output_tokens}"
    )
    print(f"vendor cost_usd: {response.usage.cost_usd}")
    print(f"computed cost_usd (section 10 formula, or vendor cost if present): {computed_cost}")


if __name__ == "__main__":
    asyncio.run(main())
