"""The LLM seam: vendor-agnostic dataclasses and the `LLMProvider` Protocol.

This file deliberately imports nothing from `openai`. All vendor
field-name knowledge (request/response shape, usage field names, error
types) stays in `openrouter.py`; this module only knows about
`LLMMessage` / `LLMUsage` / `LLMResponse`, so `FakeLLMProvider` (tests/
conftest.py) is trivial to write and swapping vendors later touches one
file, not turn.py or prompt.py (plan section 2's "one-file change"
requirement -- exercised for real in 1e's xAI -> OpenRouter swap).

Retries are NOT implemented here or in openrouter.py — they live in
`core/turn.py` (see that module's docstring for why). This module only
defines the exceptions turn.py's retry loop catches.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class LLMMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class LLMUsage:
    """Normalized usage. `output_tokens` already includes reasoning tokens,
    for any future provider whose model reports them separately (the
    current model, Cydonia, does not support a reasoning parameter and
    reports none).

    Never add a separate reasoning-token count on top of output_tokens
    when computing cost -- that double-bills. This convention dates
    back to the original xAI integration, whose Responses API returned
    output_tokens as reasoning-inclusive; it is kept as the shape's
    contract regardless of vendor.
    """

    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: Decimal | None  # vendor-reported cost, when available; None otherwise
    # Number of billable web-search requests the provider actually sent
    # for this call (0 or 1 today) -- not whether search results came
    # back, which is not something we ever inspect (see openrouter.py).
    web_search_requests: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: LLMUsage
    model: str


class LLMError(Exception):
    """A non-retryable provider failure.

    Carries only `type(exc).__name__` of the underlying vendor
    exception, never `str(exc)` — an API error body can echo prompt
    content back (e.g. a content-filter message quoting the input),
    and that would breach the privacy rule (see app/log.py). Callers
    that need to log this must log `type(error).__name__`, not `str(error)`.
    """


class LLMRetryableError(Exception):
    """A transient provider failure (429, 5xx, connection/timeout).

    `retry_after`, when the vendor supplied one (e.g. a `Retry-After`
    header), is the number of seconds turn.py's retry loop should wait
    before trying again; None means "use the loop's own backoff".
    """

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("retryable LLM provider error")
        self.retry_after = retry_after


class LLMProvider(Protocol):
    async def complete(
        self, messages: list[LLMMessage], *, conversation_id: str, web_search: bool = False
    ) -> LLMResponse: ...

    async def close(self) -> None: ...
