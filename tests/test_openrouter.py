"""OpenRouter response handling: body-level errors and usage extraction.

These use the real `ChatCompletion` model from the openai SDK rather
than a hand-rolled stub, so they exercise the same `extra="allow"`
pydantic behaviour the live response relies on -- a stub would happily
expose fields the SDK might drop.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from openai.types.chat import ChatCompletion

from app.llm.openrouter import (
    OpenRouterProvider,
    _extract_text,
    _extract_usage,
    _raise_for_body_error,
)
from app.llm.provider import LLMError, LLMMessage, LLMRetryableError

_BASE = {"id": "gen-x", "created": 1, "model": "m", "object": "chat.completion"}


def _response(**overrides) -> ChatCompletion:
    return ChatCompletion.construct(**{**_BASE, "choices": [], **overrides})


def _ok_response(content: str = "привет", **usage_fields) -> ChatCompletion:
    usage = {"prompt_tokens": 120, "completion_tokens": 40, **usage_fields}
    return ChatCompletion.construct(
        **{
            **_BASE,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": usage,
        }
    )


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_transient_body_error_is_retryable(code):
    """OpenRouter commits the 200 before the provider runs, so upstream
    capacity failures arrive in the body with an empty choices list.
    Cydonia has one provider, so a retry is the only recovery there is
    -- these must not collapse into the non-retryable path."""
    response = _response(error={"code": code, "message": "Provider returned error"})
    with pytest.raises(LLMRetryableError):
        _raise_for_body_error(response)


@pytest.mark.parametrize("code", [400, 402, 403, 404])
def test_permanent_body_error_is_not_retryable(code):
    response = _response(error={"code": code, "message": "Bad request"})
    with pytest.raises(LLMError):
        _raise_for_body_error(response)


def test_body_error_never_leaks_the_upstream_message():
    """Privacy rule: an upstream error body can echo prompt content
    back (a content filter quoting the input, for instance), so only
    the code may travel into the exception."""
    secret = "пользователь сказал что-то личное"
    response = _response(error={"code": 400, "message": secret})
    with pytest.raises(LLMError) as excinfo:
        _raise_for_body_error(response)
    assert secret not in str(excinfo.value)


def test_unparseable_error_code_is_not_retryable():
    response = _response(error={"message": "something went wrong"})
    with pytest.raises(LLMError):
        _raise_for_body_error(response)


def test_successful_response_passes_through():
    _raise_for_body_error(_ok_response())  # must not raise


def test_extract_text_and_usage_on_a_normal_response():
    response = _ok_response(content="я на связи", cost=0.000123)
    assert _extract_text(response) == "я на связи"

    usage = _extract_usage(response)
    assert usage.input_tokens == 120
    assert usage.output_tokens == 40
    assert usage.cached_tokens == 0
    # Decimal(str(cost)), never Decimal(float) -- the latter drags in the
    # float's binary representation error from the sixth decimal on.
    assert usage.cost_usd == Decimal("0.000123")


def test_usage_without_a_cost_field_falls_back_to_the_local_formula():
    """Cydonia reports supports_implicit_caching: false, and `cost` is a
    non-standard field. cost_usd=None is what tells compute_cost to use
    the configured prices instead."""
    usage = _extract_usage(_ok_response())
    assert usage.cost_usd is None
    assert usage.cached_tokens == 0


def test_missing_usage_block_does_not_crash():
    response = ChatCompletion.construct(
        **{
            **_BASE,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ок"}}],
        }
    )
    usage = _extract_usage(response)
    assert (usage.input_tokens, usage.cached_tokens, usage.output_tokens) == (0, 0, 0)
    assert usage.cost_usd is None


def test_empty_choices_never_yields_a_silent_empty_reply():
    with pytest.raises(LLMError):
        _extract_text(_response())


class _FakeCompletions:
    def __init__(self, response):
        self._response = response

    async def create(self, **kwargs):
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.chat = type("_Chat", (), {"completions": _FakeCompletions(response)})()


def _provider_returning(response) -> OpenRouterProvider:
    provider = OpenRouterProvider(
        api_key="test-key",
        model="thedrummer/cydonia-24b-v4.1",
        max_tokens=700,
        temperature=0.9,
        data_collection="deny",
    )
    provider._client = _FakeClient(response)
    return provider


async def test_transient_body_error_stays_retryable_through_complete():
    """Guards the call site, not just the classifier: _raise_for_body_error
    must run OUTSIDE complete()'s try block. Inside it, the catch-all
    `except Exception` would flatten LLMRetryableError into a
    non-retryable LLMError and turn.py would stop retrying -- the
    classifier would still be correct and the bug invisible to the
    tests above."""
    provider = _provider_returning(_response(error={"code": 429, "message": "Provider returned error"}))
    with pytest.raises(LLMRetryableError):
        await provider.complete([LLMMessage(role="user", content="привет")], conversation_id="anchor-main")


async def test_successful_call_through_complete_returns_the_reply():
    provider = _provider_returning(_ok_response(content="я на связи", cost=0.000123))
    response = await provider.complete(
        [LLMMessage(role="user", content="привет")], conversation_id="anchor-main"
    )
    assert response.text == "я на связи"
    assert response.model == "thedrummer/cydonia-24b-v4.1"
    assert response.usage.cost_usd == Decimal("0.000123")


class _CapturingCompletions:
    def __init__(self, response):
        self._response = response
        self.received_kwargs: dict = {}

    async def create(self, **kwargs):
        self.received_kwargs = kwargs
        return self._response


class _CapturingClient:
    def __init__(self, response):
        self.chat = type("_Chat", (), {"completions": _CapturingCompletions(response)})()


def _provider_and_client(response) -> tuple[OpenRouterProvider, _CapturingClient]:
    provider = OpenRouterProvider(
        api_key="test-key",
        model="thedrummer/cydonia-24b-v4.1",
        max_tokens=700,
        temperature=0.9,
        data_collection="deny",
    )
    client = _CapturingClient(response)
    provider._client = client
    return provider, client


async def test_ordinary_call_sends_no_plugins_and_no_tools():
    """Safety invariant 4: this bot has no tool-calling surface, and
    (since milestone 4a removed /search) no request-shaping surface
    either -- an ordinary call sends none of plugins/tools/tool_choice/
    functions, unconditionally."""
    provider, client = _provider_and_client(_ok_response())
    await provider.complete([LLMMessage(role="user", content="привет")], conversation_id="anchor-main")

    kwargs = client.chat.completions.received_kwargs
    assert "plugins" not in kwargs["extra_body"]
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs
    assert "functions" not in kwargs
