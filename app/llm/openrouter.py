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
"""

from __future__ import annotations

import logging
from decimal import Decimal

import openai
from openai import AsyncOpenAI

from app.llm.provider import LLMError, LLMMessage, LLMResponse, LLMRetryableError, LLMUsage

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT_SECONDS = 90.0

# A constant, not a setting: Exa is the only search engine OpenRouter's
# `web` plugin is used with here, and adding a knob for it would only
# grow the config surface without a second value ever exercising it.
WEB_SEARCH_ENGINE = "exa"

# Overrides OpenRouter's default search_prompt, which instructs the
# model to cite sources as markdown links. This is a persona bot: a
# reply that suddenly prints "[some site](https://...)" breaks
# character on every searched turn, so the model is told to use the
# facts naturally instead and never surface the search mechanics.
WEB_SEARCH_PROMPT = (
    "Ниже приведены свежие результаты поиска в интернете на сегодня; "
    "используй факты из них естественно, как будто ты уже это знаешь, "
    "и никогда не печатай ссылки, URL, домены или сноски-цитаты."
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


def _extract_usage(response, *, web_search_requests: int = 0) -> LLMUsage:
    """`web_search_requests` is set by the caller from what WE SENT (whether
    the `web` plugin was in the request), never from anything in the
    response -- annotations coming back or not is irrelevant to what we
    were billed for, and we don't want a flaky "no results found" case
    to silently under-report a request that OpenRouter still charged us for.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return LLMUsage(
            input_tokens=0,
            cached_tokens=0,
            output_tokens=0,
            cost_usd=None,
            web_search_requests=web_search_requests,
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
        web_search_requests=web_search_requests,
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
        web_search_max_results: int,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = build_client(api_key) if client is None else client
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._data_collection = data_collection
        self._web_search_max_results = web_search_max_results

    async def complete(
        self, messages: list[LLMMessage], *, conversation_id: str, web_search: bool = False
    ) -> LLMResponse:
        # conversation_id is part of the Protocol's call shape but unused
        # here: OpenRouter has no prompt-cache-key field, and Cydonia
        # reports supports_implicit_caching: false, so there is nothing
        # to key a cache on -- cached_tokens will always come back 0.
        extra_body = {"provider": {"data_collection": self._data_collection}}
        if web_search:
            extra_body["plugins"] = [
                {
                    "id": "web",
                    "engine": WEB_SEARCH_ENGINE,
                    "max_results": self._web_search_max_results,
                    "search_prompt": WEB_SEARCH_PROMPT,
                }
            ]
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                extra_body=extra_body,
                # No HTTP-Referer / X-Title headers here on purpose: both
                # are optional OpenRouter attribution headers that would
                # list this private bot on OpenRouter's public
                # leaderboard. Never pass `tools` either -- this bot has
                # no tool-calling surface (safety invariant 4). `plugins`
                # (above, opt-in only) is NOT `tools`: OpenRouter performs
                # the web search itself and injects the excerpts straight
                # into the prompt, so the model is never offered a
                # callable and safety invariant 4 stays intact.
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
        text = _extract_text(response)
        usage = _extract_usage(response, web_search_requests=1 if web_search else 0)
        return LLMResponse(text=text, usage=usage, model=self._model)

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
