"""OpenRouter implementation of `LLMProvider`, via the `openai` SDK's Chat
Completions API.

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

2c adds structured outputs. OpenRouter reports that Cydonia's only
provider (Parasail) supports `structured_outputs`, but a *declared*
capability is not a verified one, which is why `structured_outputs`
is a constructor flag fed from LLM_STRUCTURED_OUTPUTS: if a live call
turns out to reject `response_format`, one env var turns it off
without a code change, and app/core/extract.py still parses and
validates the reply exactly as it did before. Strict schema is a
reliability feature here, never a correctness one.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import openai
from openai import AsyncOpenAI

from app.llm.provider import (
    Citation,
    JSONSchema,
    LLMError,
    LLMMessage,
    LLMResponse,
    LLMRetryableError,
    LLMUsage,
    WebSearch,
)

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT_SECONDS = 90.0

# Retained for the Phase 4 research search module (app/research/search.py,
# milestone 4c); unused since milestone 4a removed /search. Kept as a
# constant rather than a setting: Exa is the only search engine that
# module will use, and a knob for it would only grow the config surface
# without a second value ever exercising it.
WEB_SEARCH_ENGINE = "exa"

# Overrides OpenRouter's default search_prompt, which tells the model to
# cite its sources as markdown links in the reply. We are not reading
# the reply: app/research/search.py keeps the `url_citation`
# annotations and throws the prose away (plan section 6). So the prompt
# asks for the shortest possible completion instead -- the annotations
# are attached by OpenRouter from the search itself, not written by the
# model, so nothing is lost by the model saying almost nothing.
#
# This does not make the completion free: the plugin injects the search
# excerpts into the prompt as input tokens either way. It only stops us
# paying for output we discard.
RESEARCH_SEARCH_PROMPT = (
    "Ниже результаты поиска. Ответь одним словом: готово."
)


def build_client(api_key: str) -> AsyncOpenAI:
    """Construct the AsyncOpenAI client pointed at OpenRouter.

    max_retries=0: see module docstring — retries belong to turn.py only.
    """
    return AsyncOpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        max_retries=0,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _extract_text(response) -> str:
    """response.choices[0].message.content, guarded against an empty
    choices list and a falsy (None or empty-string) content.

    Raising here on an unexpected shape is deliberate: a silently empty
    reply would be indistinguishable from the model actually having
    nothing to say, and that must never happen unnoticed (hard
    requirement: never return an empty reply silently).
    """
    choices = getattr(response, "choices", None) or []
    if choices:
        content = choices[0].message.content
        if content:
            return content
    raise LLMError("no message content in OpenRouter response")


def _extract_citations(response) -> tuple[Citation, ...]:
    """The `url_citation` annotations on the assistant message.

    OpenRouter standardises these across every search engine into the
    OpenAI Chat Completion annotation shape (verified against
    https://openrouter.ai/docs/features/web-search on 2026-09-22):

        {"type": "url_citation",
         "url_citation": {"url": ..., "title": ..., "content": ...}}

    Read defensively through both attribute and mapping access: the
    openai SDK models what it knows and leaves the rest in
    `model_extra`, and `annotations` is an OpenRouter addition the SDK
    may or may not have a field for on any given version.

    **`content` is deliberately not read.** It is a two-to-four-thousand
    character excerpt of the page, chosen by a search engine, and plan
    section 2 says the provider's snippets are never distill input --
    we fetch the page ourselves. Not carrying it past this function is
    what makes that structural rather than a promise.

    A malformed annotation is skipped rather than raising: this list is
    a hint about where to look next, and one bad entry is not a reason
    to fail a job that has other candidates.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ()
    message = choices[0].message
    raw = getattr(message, "annotations", None)
    if raw is None:
        extra = getattr(message, "model_extra", None) or {}
        raw = extra.get("annotations")
    if not isinstance(raw, (list, tuple)):
        return ()

    citations: list[Citation] = []
    for item in raw:
        payload = _field(item, "url_citation")
        if payload is None:
            continue
        url = _field(payload, "url")
        if not isinstance(url, str) or not url:
            continue
        title = _field(payload, "title")
        citations.append(Citation(url=url, title=title if isinstance(title, str) else None))
    return tuple(citations)


def _field(obj, name: str):
    """One field of a value the SDK may have modelled or may have left raw."""
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is not None:
        return value
    extra = getattr(obj, "model_extra", None) or {}
    return extra.get(name) if isinstance(extra, dict) else None


def _raise_for_body_error(response) -> None:
    """Classify an `error` object carried in an otherwise-successful body.

    OpenRouter commits the 200 before the upstream provider runs, so a
    provider failure arrives as an `error` object in the response body
    with an empty `choices` list, not as an HTTP status. Without this,
    every such failure falls through to _extract_text and becomes a
    non-retryable LLMError -- and the commonest cause by far is
    transient upstream capacity (code 429/5xx), which is exactly what
    turn.py's retry loop exists for. That matters more here than it
    would for most vendors: Cydonia has exactly one provider
    (Parasail), so there is no second endpoint to fall back to and a
    retry is the only recovery available.

    The error's `message` is deliberately never included: an upstream
    body can echo prompt content back (privacy rule, see app/log.py).
    """
    error = getattr(response, "error", None)
    if not error:
        return
    raw_code = error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        code = None
    if code is not None and (code == 429 or code >= 500):
        raise LLMRetryableError(retry_after=None)
    raise LLMError(f"openrouter body error code={code}")


def _extract_usage(response) -> LLMUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return LLMUsage(
            input_tokens=0,
            cached_tokens=0,
            output_tokens=0,
            cost_usd=None,
        )
    # prompt_tokens_details is itself optional on some responses/models.
    details = getattr(usage, "prompt_tokens_details", None)
    cached_tokens = getattr(details, "cached_tokens", 0) or 0
    # `cost` is a non-standard field OpenRouter adds to the response;
    # the openai SDK's pydantic models use extra="allow" so it survives
    # onto the object without being a declared field, hence getattr.
    cost = getattr(usage, "cost", None)
    cost_usd = Decimal(str(cost)) if cost is not None else None
    return LLMUsage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        cached_tokens=cached_tokens,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cost_usd=cost_usd,
    )


class OpenRouterProvider:
    """`LLMProvider` backed by OpenRouter (model: thedrummer/cydonia-24b-v4.1)
    via the openai SDK's Chat Completions API.

    2a: `client` lets a second instance (the cheap/background provider)
    share the first one's AsyncOpenAI client, and therefore one
    connection pool, instead of opening a second one for what is the
    same host and the same credential. When a client is injected this
    instance does not own it, and close() is a no-op -- whoever built
    the client closes it (app/main.py). `api_key` is then unused and may
    be empty.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        temperature: float,
        data_collection: str,
        client: AsyncOpenAI | None = None,
        structured_outputs: bool = True,
    ) -> None:
        self._owns_client = client is None
        self._client = build_client(api_key) if client is None else client
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._data_collection = data_collection
        self._structured_outputs = structured_outputs

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        conversation_id: str,
        json_schema: JSONSchema | None = None,
        web_search: WebSearch | None = None,
    ) -> LLMResponse:
        # conversation_id is part of the Protocol's call shape but unused
        # here: OpenRouter has no prompt-cache-key field, and Cydonia
        # reports supports_implicit_caching: false, so there is nothing
        # to key a cache on -- cached_tokens will always come back 0.
        extra_body = {"provider": {"data_collection": self._data_collection}}
        request: dict = {}
        if json_schema is not None and self._structured_outputs:
            # require_parameters refuses a provider that would ignore
            # response_format and hand back prose. Failing loudly beats
            # silently returning something the validator then rejects
            # for reasons that look like the model's fault.
            extra_body["provider"]["require_parameters"] = True
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.name,
                    "strict": json_schema.strict,
                    "schema": json_schema.schema,
                },
            }
        if web_search is not None:
            # 4c. The ONE request-shaping branch that hands anything to a
            # third party, and the only place in the tree a `plugins`
            # payload is built. tests/test_web_search_isolation.py pins
            # both halves: that no module outside this file constructs
            # the key, and that exactly one call site above the seam
            # passes a WebSearch at all.
            #
            # `plugins` is not `tools`: OpenRouter runs the search
            # itself and injects the results into the prompt. The model
            # is never given anything it can call (safety invariant 4).
            #
            # engine is pinned to Exa rather than left to default.
            # LLM_MODEL_SAFETY is a Google model, and OpenRouter's docs
            # say Google's native search does not support domain
            # filtering -- with the default engine it silently falls
            # back to Exa when filters are set, and with
            # engine="native" it returns a 400. Naming Exa makes the
            # behaviour, and the $0.007 per-request fee, the same
            # whatever LLM_MODEL_SAFETY is pointed at next.
            plugin: dict = {
                "id": "web",
                "engine": WEB_SEARCH_ENGINE,
                "max_results": web_search.max_results,
                "search_prompt": RESEARCH_SEARCH_PROMPT,
            }
            if web_search.include_domains:
                plugin["include_domains"] = list(web_search.include_domains)
            extra_body["plugins"] = [plugin]

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                extra_body=extra_body,
                **request,
                # No HTTP-Referer / X-Title headers here on purpose: both
                # are optional OpenRouter attribution headers that would
                # list this private bot on OpenRouter's public
                # leaderboard. Never pass `tools` either -- this bot has
                # no tool-calling surface (safety invariant 4).
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

        # Outside the try on purpose: `except Exception` above would
        # otherwise flatten a LLMRetryableError raised here into a
        # non-retryable LLMError.
        _raise_for_body_error(response)
        # A searched call is allowed to come back with no prose at all:
        # RESEARCH_SEARCH_PROMPT asks for one word, some models answer
        # with none, and the annotations -- the only part we want -- are
        # attached by OpenRouter regardless. _extract_text raises on an
        # empty completion, which is right for every other caller and
        # wrong for this one.
        text = "" if web_search is not None else _extract_text(response)
        usage = _extract_usage(response)
        return LLMResponse(
            text=text,
            usage=usage,
            model=self._model,
            citations=_extract_citations(response),
        )

    async def close(self) -> None:
        """Close the client, unless it was injected and belongs to someone else."""
        if self._owns_client:
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
