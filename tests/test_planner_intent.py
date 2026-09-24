"""app/planner/intent.py: the P4 prefilter and safety-model intent call.

Shaped like tests/test_planner_parse.py for the provider-facing half
(validation reuse, `.calls == 0` on a miss) and tests/test_welfare.py's
`ScriptedWelfare` pattern for dispatching a single FakeLLMProvider by
system prompt -- app/core/turn.py runs this alongside welfare.classify
on the *same* safety_provider, so a fake standing in for it must be
able to answer both.
"""

from __future__ import annotations

import datetime

import pytest

from app.config import Settings
from app.planner import actions as planner_actions
from app.planner import intent as planner_intent
from conftest import FakeLLMProvider

pytestmark = pytest.mark.asyncio

TZ = "Europe/Paris"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, PLANNER_INTENT=True, **overrides)


# --- the prefilter -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "напомни завтра купить молоко",
        "запиши задачу на понедельник",
        "добавь в планер встречу",
        "добавь в календарь встречу в 18:00",
        "запланируй звонок на завтра",
        "remind me to call mom",
        "please add to calendar dentist at 10am",
        "schedule a meeting for tomorrow",
    ],
)
def test_prefilter_hits_on_trigger_phrases(text):
    assert planner_intent.prefilter_hit(text)


@pytest.mark.parametrize(
    "text",
    [
        "как дела?",
        "сегодня было тяжело на работе",
        "думаю о планах на жизнь вообще",
        "",
        None,
    ],
)
def test_prefilter_misses_ordinary_chat(text):
    assert not planner_intent.prefilter_hit(text)


# --- detect(): gating ----------------------------------------------------


async def test_prefilter_miss_makes_no_provider_call(frozen_clock, fake_llm_provider):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    result, response = await planner_intent.detect(
        fake_llm_provider, _settings(), clock, user_text="как дела?", timezone=TZ,
    )
    assert result is None
    assert response is None
    assert fake_llm_provider.calls == 0


async def test_flag_off_makes_no_provider_call_even_on_a_prefilter_hit(
    frozen_clock, fake_llm_provider
):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = Settings(_env_file=None, PLANNER_INTENT=False)
    result, response = await planner_intent.detect(
        fake_llm_provider, settings, clock, user_text="напомни купить молоко", timezone=TZ,
    )
    assert result is None
    assert response is None
    assert fake_llm_provider.calls == 0


# --- detect(): a hit ------------------------------------------------------


async def test_task_intent_hit_produces_a_create_task_result(frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    provider = FakeLLMProvider(
        text='{"kind": "task", "title": "Купить молоко", "due_date": "2026-09-24", '
        '"date": null, "start_time": null, "end_time": null, "all_day": false}'
    )
    result, response = await planner_intent.detect(
        provider, _settings(), clock, user_text="напомни купить молоко завтра", timezone=TZ,
    )
    assert result is not None
    assert result.kind == planner_actions.CREATE_TASK
    assert result.payload == {"title": "Купить молоко", "due_date": "2026-09-24"}
    assert response is not None
    assert provider.calls == 1


async def test_event_intent_hit_produces_a_create_event_result(frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    provider = FakeLLMProvider(
        text='{"kind": "event", "title": "Встреча с Аней", "due_date": null, '
        '"date": "2026-09-24", "start_time": "18:00", "end_time": "19:00", "all_day": false}'
    )
    result, response = await planner_intent.detect(
        provider, _settings(), clock,
        user_text="запланируй встречу с Аней завтра в 18:00", timezone=TZ,
    )
    assert result is not None
    assert result.kind == planner_actions.CREATE_EVENT
    assert result.payload["title"] == "Встреча с Аней"
    assert result.payload["all_day"] is False


async def test_kind_none_produces_no_result(frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    provider = FakeLLMProvider(
        text='{"kind": "none", "title": null, "due_date": null, "date": null, '
        '"start_time": null, "end_time": null, "all_day": false}'
    )
    result, response = await planner_intent.detect(
        provider, _settings(), clock, user_text="напомни, как тебя зовут?", timezone=TZ,
    )
    assert result is None
    assert response is not None, "still billed, even though nothing was proposed"


async def test_an_oversized_title_is_rejected_like_parse_pys_own_validation(frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    long_title = "A" * 250
    provider = FakeLLMProvider(
        text=f'{{"kind": "task", "title": "{long_title}", "due_date": null, '
        '"date": null, "start_time": null, "end_time": null, "all_day": false}}'
    )
    result, response = await planner_intent.detect(
        provider, _settings(), clock, user_text="напомни что-то очень длинное", timezone=TZ,
    )
    assert result is None
    assert response is not None


async def test_unparseable_reply_fails_open(frozen_clock):
    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    provider = FakeLLMProvider(text="не json вовсе")
    result, response = await planner_intent.detect(
        provider, _settings(), clock, user_text="запиши что-нибудь", timezone=TZ,
    )
    assert result is None
    assert response is not None


async def test_a_provider_error_fails_open_without_raising(frozen_clock):
    from app.llm.provider import LLMError

    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    provider = FakeLLMProvider(raises=[LLMError("boom")])
    result, response = await planner_intent.detect(
        provider, _settings(), clock, user_text="запиши что-нибудь", timezone=TZ,
    )
    assert result is None
    assert response is None


async def test_a_timeout_fails_open_without_raising(frozen_clock):
    import asyncio

    class _SlowProvider(FakeLLMProvider):
        async def complete(self, *args, **kwargs):
            await asyncio.sleep(10)
            return await super().complete(*args, **kwargs)  # pragma: no cover

    clock = frozen_clock(2026, 9, 23, 9, 0, tz=TZ)
    settings = _settings(PLANNER_INTENT_TIMEOUT_SECONDS=0.01)
    result, response = await planner_intent.detect(
        _SlowProvider(), settings, clock, user_text="напомни что-нибудь", timezone=TZ,
    )
    assert result is None
    assert response is None
