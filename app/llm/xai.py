"""xAI implementation of `LLMProvider`, via the `openai` SDK's Responses API.

All vendor-specific knowledge (field names, error types, request shape)
lives here; app/llm/provider.py and everything above it never imports
`openai`.

Retries live in core/turn.py, not here or in the SDK — build the client
with `max_retries=0` explicitly. The SDK's own default (max_retries=2)
would otherwise stack with turn.py's loop: turn.py retries up to 2
times, and if each of those 3 attempts silently retried twice more
inside the SDK, one turn could cost up to 9 HTTP attempts. Explicit
max_retries=0 makes the intent visible instead of inherited from a
default, and it is also the only way `FakeLLMProvider` can exercise
turn.py's retry behavior in tests without a real network: if the SDK
retried internally, a "raise twice then succeed" fake would never be
reachable from turn.py's own loop.

timeout=90.0 overrides the SDK's 10-minute default, which is far too
long to hold a Telegram chat turn open.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import openai
from openai import AsyncOpenAI

from app.llm.provider import LLMError, LLMMessage, LLMResponse, LLMRetryableError, LLMUsage

logger = logging.getLogger(__name__)

XAI_BASE_URL = "https://api.x.ai/v1"
REQUEST_TIMEOUT_SECONDS = 90.0


def build_client(api_key: str) -> AsyncOpenAI:
    """Construct the AsyncOpenAI client pointed at xAI.

    max_retries=0: see module docstring — retries belong to turn.py only.
    """
    return AsyncOpenAI(
        api_key=api_key,
        base_url=XAI_BASE_URL,
        max_retries=0,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _extract_text(response) -> str:
    """Traverse response.output -> type=="message" -> content[] -> type=="output_text".

    There is no top-level `output_text` shortcut on the REST response
    shape we get back from xAI, so this walk is required. Raising here
    on an unexpected shape is deliberate: a silently empty reply would
    be indistinguishable from the model actually having nothing to say,
    and that must never happen unnoticed (hard requirement: never
    return an empty reply silently).
    """
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "output_text":
                text = getattr(part, "text", None)
                if text:
                    return text
    raise LLMError("no output_text found in xAI response")


def _extract_usage(response) -> LLMUsage:
    usage = response.usage
    cached_tokens = getattr(usage.input_tokens_details, "cached_tokens", 0) or 0
    # tokens_out already includes reasoning tokens (usage.output_tokens is
    # documented as reasoning-inclusive) -- never add
    # output_tokens_details.reasoning_tokens on top, that double-bills.
    cost_in_nano_usd = getattr(usage, "cost_in_nano_usd", None)
    cost_usd = Decimal(cost_in_nano_usd) / Decimal(1_000_000_000) if cost_in_nano_usd is not None else None
    return LLMUsage(
        input_tokens=usage.input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost_usd,
    )


class XAIProvider:
    """`LLMProvider` backed by xAI Grok via the openai SDK's Responses API."""

    def __init__(self, api_key: str, model: str, reasoning_effort: str) -> None:
        self._client = build_client(api_key)
        self._model = model
        self._reasoning_effort = reasoning_effort

    async def complete(self, messages: list[LLMMessage], *, conversation_id: str) -> LLMResponse:
        try:
            response = await self._client.responses.create(
                model=self._model,
                input=[{"role": m.role, "content": m.content} for m in messages],
                reasoning={"effort": self._reasoning_effort},
                # store=False is the privacy switch: `store` defaults to
                # True with 30-day server-side retention on xAI's side.
                # Do NOT remove this — we keep our own transcript in
                # Postgres and never want a copy sitting on the vendor's
                # servers. Removing it silently reintroduces persistent
                # storage of message content outside our control.
                store=False,
                prompt_cache_key=conversation_id,
            )
        except openai.APIConnectionError:
            # Covers both APIConnectionError and its subclass
            # APITimeoutError; neither carries a response to read
            # Retry-After from.
            raise LLMRetryableError(retry_after=None) from None
        except openai.RateLimitError as exc:
            raise LLMRetryableError(retry_after=_retry_after(exc)) from None
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMRetryableError(retry_after=_retry_after(exc)) from None
            raise LLMError(type(exc).__name__) from None
        except Exception as exc:  # noqa: BLE001 - any other vendor failure is non-retryable
            raise LLMError(type(exc).__name__) from None

        text = _extract_text(response)
        usage = _extract_usage(response)
        return LLMResponse(text=text, usage=usage, model=self._model)

    async def close(self) -> None:
        await self._client.close()


def _retry_after(exc: openai.APIStatusError) -> float | None:
    """Read Retry-After from the response headers, when present."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    header = response.headers.get("retry-after")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None
