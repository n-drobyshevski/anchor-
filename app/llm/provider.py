"""The LLM seam: vendor-agnostic dataclasses and the `LLMProvider` Protocol.

This file deliberately imports nothing from `openai`. All xAI field-name
knowledge (Responses API shape, usage field names, error types) stays in
`xai.py`; this module only knows about `LLMMessage` / `LLMUsage` /
`LLMResponse`, so `FakeLLMProvider` (tests/conftest.py) is trivial to
write and swapping vendors later touches one file, not turn.py or
prompt.py (plan section 2's "one-file change" requirement).

Retries are NOT implemented here or in xai.py — they live in
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
    """Normalized usage. `output_tokens` already includes reasoning tokens.

    Never add a separate reasoning-token count on top of output_tokens
    when computing cost — see xai.py's docstring for the double-billing
    trap this shape is designed to avoid.
    """

    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: Decimal | None  # vendor-reported cost, when available; None otherwise


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
        self, messages: list[LLMMessage], *, conversation_id: str
    ) -> LLMResponse: ...

    async def close(self) -> None: ...
