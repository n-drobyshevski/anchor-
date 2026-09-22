"""The LLM seam: vendor-agnostic dataclasses and the `LLMProvider` Protocol.

This file deliberately imports nothing from `openai`. All vendor
field-name knowledge (request/response shape, usage field names, error
types) stays in `openrouter.py`; this module only knows about
`LLMMessage` / `LLMUsage` / `LLMResponse`, so `FakeLLMProvider` (tests/
conftest.py) is trivial to write and swapping vendors later touches one
file, not turn.py or prompt.py (plan section 2's "one-file change"
requirement -- exercised for real in 1e's xAI -> OpenRouter swap).

4c adds `WebSearch` and `Citation`. They live here rather than in
`app/research/` for the same reason everything else in this file does:
the research package must not know how a search request is spelled on
the wire. Only one call site in the whole tree may pass a `WebSearch`
at all, and tests/test_web_search_isolation.py names it.

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


@dataclass(frozen=True)
class WebSearch:
    """A request for **URL discovery**, not for grounded prose (4c).

    Vendor-agnostic on purpose, like `JSONSchema`: `openrouter.py` turns
    this into the `web` plugin's request shape and a future vendor would
    turn it into whatever that vendor wants.

    `include_domains` is passed to the provider as a hint and is never
    trusted: `app/research/search.py` re-filters every returned URL
    against the packet allowlist in code, because a provider-side filter
    is a request and the allowlist is a rule (phase-4 plan section 2).
    """

    max_results: int = 5
    include_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class Citation:
    """One source the provider says it consulted.

    The **only** part of a searched response `app/research/search.py`
    keeps. The model's prose is discarded entirely (plan section 6), so
    no text a search engine chose ever reaches a prompt with state in
    it -- and no URL ever comes from something the model wrote.
    """

    url: str
    title: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: LLMUsage
    model: str
    citations: tuple[Citation, ...] = ()


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


@dataclass(frozen=True)
class JSONSchema:
    """A strict JSON schema for a structured-output call (2c).

    Vendor-agnostic on purpose: `openrouter.py` turns this into
    OpenAI's `response_format={"type": "json_schema", ...}` shape, and a
    future vendor would turn it into whatever that vendor wants.
    Nothing above the seam knows how it is transmitted.

    `strict` asks the provider to constrain decoding to the schema.
    It is a *reliability* feature, never a correctness one: every field
    is validated in code after parsing regardless (app/core/extract.py),
    because a model that can emit a field is a model that can emit the
    wrong field.
    """

    name: str
    schema: dict
    strict: bool = True


class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        conversation_id: str,
        json_schema: JSONSchema | None = None,
        web_search: WebSearch | None = None,
    ) -> LLMResponse: ...

    async def close(self) -> None: ...
