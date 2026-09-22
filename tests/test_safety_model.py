"""The safety model split and its backstop (hardening milestone H2).

Three things are under test, and they are separable on purpose:

- **The term list** (`app/core/welfare_terms.py`) is pure and gets a
  table-driven test. It is the one piece whose failure mode is silent:
  a stem that never matches looks exactly like a user who never said
  the word.
- **The seam** -- `classify()` reporting *what happened to the call* as
  well as what it concluded. Before H2 a dead classifier and a quiet
  week were the same value.
- **The backstop firing**, and only when the model gave nothing usable.
  A keyword matcher that ran on every turn would be a different, worse
  product; this one is a fallback and has to stay one.

Plus the observability row and the price triple, which are small but
are what make the rest checkable from a phone.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal

import pytest
from aiogram import Bot
from sqlalchemy import select

from app.config import Settings
from app.core import safety_events, turn, welfare, welfare_terms
from app.core.clock import FrozenClock, combine_local
from app.core.spend import price_triple_for
from app.db.models import SafetyEvent, TelegramUpdate, UserState
from conftest import FakeLLMProvider, FakeSession

TIMEZONE = "Europe/Paris"
CHAT_ID = 4242
DAY = datetime.date(2026, 9, 23)


def _settings(**overrides) -> Settings:
    base = dict(TZ_DEFAULT=TIMEZONE, DAILY_USD_CAP=10.0, WELFARE_MIN_CONF=0.6)
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _at(hour: int, minute: int = 0) -> FrozenClock:
    return FrozenClock(combine_local(DAY, datetime.time(hour, minute), TIMEZONE))


def _bot() -> tuple[Bot, FakeSession]:
    fake = FakeSession()
    return Bot(token="123456:TESTTOKEN", session=fake), fake


async def _seed(sessionmaker, *update_ids: int, **state) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE, **state))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


async def _events(sessionmaker) -> list[SafetyEvent]:
    async with sessionmaker() as session:
        result = await session.execute(select(SafetyEvent).order_by(SafetyEvent.id))
        return list(result.scalars().all())


# --- the term list -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Russian, across inflections the stems have to reach
        "я думаю про суицид",
        "суицидальные мысли не уходят",
        "хочу умереть",
        "не хочу жить",
        "я решил покончить с собой",
        "хочу вскрыть вены",
        # ё-folding: the term list is written with е
        "решил свести счёты с жизнью",
        # French
        "je veux me tuer",
        "j'ai envie de mourir",
        "je pense au suicide",
        # English
        "I want to kill myself",
        "thinking about self-harm",
        "I just want to die",
    ],
)
def test_every_language_is_covered(text):
    assert welfare_terms.hit(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # The ordinary register of a demanding persona bot. None of this
        # may trip the backstop -- it runs unsupervised, by definition,
        # because it only runs when the classifier already failed.
        "не сплю третью неделю, всё бесит",
        "стоп, мне реально хреново, это не игра",
        "отчёт не готов, я устал и злюсь",
        "убил весь день на созвоны",
        "этот дедлайн меня убивает",
        "ок, сделаю до вечера",
        "жить стало легче, честно",
        "",
    ],
)
def test_ordinary_frustration_does_not_trip_it(text):
    assert welfare_terms.hit(text) is False


def test_it_reads_every_text_it_is_given():
    """Variadic, because the caller passes the message plus recent turns."""
    assert welfare_terms.hit("привет", None, "хочу умереть") is True
    assert welfare_terms.hit("привет", None, "как дела") is False
    assert welfare_terms.hit() is False


def test_it_never_reveals_what_matched():
    """The privacy rule, as a test. A function that returned the term
    would be one refactor away from that term reaching a log line."""
    assert welfare_terms.hit("хочу умереть") is True
    public = [name for name in dir(welfare_terms) if not name.startswith("_")]
    assert "hit" in public
    # Nothing public returns or yields a matched substring.
    assert not any(name.startswith(("find", "match", "search", "which")) for name in public)


def test_a_stem_matches_only_at_a_word_boundary():
    """Prefix matching, not substring matching -- otherwise an innocent
    word containing a stem would fire."""
    assert welfare_terms.hit("суицидальный") is True
    assert welfare_terms.hit("психосуицидология") is False


# --- the seam: classify() reports what happened --------------------------


class _Boom(FakeLLMProvider):
    async def complete(self, *a, **kw):
        raise RuntimeError("provider is down")


class _Hangs(FakeLLMProvider):
    async def complete(self, *a, **kw):
        await asyncio.sleep(10)


async def test_a_good_verdict_is_ok():
    provider = FakeLLMProvider(text='{"level": "none", "confidence": 0.9}')
    result = await welfare.classify(provider, _settings(), [], "привет")
    assert result.outcome == welfare.OK
    assert result.verdict.level == "none"
    assert result.verdict.usable is True


async def test_unparseable_output_is_parse_fail_not_a_quiet_none():
    """The bug this milestone exists for: before H2 both of these were
    Verdict('none', 0.0) and nothing could tell them apart."""
    provider = FakeLLMProvider(text="сложно сказать, если честно")
    result = await welfare.classify(provider, _settings(), [], "привет")
    assert result.outcome == welfare.PARSE_FAIL
    assert result.verdict.level == "none"  # still fails open
    assert result.verdict.usable is False
    assert result.response is not None  # billed, so still ledgered


async def test_a_provider_error_is_error():
    result = await welfare.classify(_Boom(), _settings(), [], "привет")
    assert result.outcome == welfare.ERROR
    assert result.response is None


async def test_a_timeout_is_timeout():
    result = await welfare.classify(
        _Hangs(), _settings(WELFARE_TIMEOUT_SECONDS=0.01), [], "привет"
    )
    assert result.outcome == welfare.TIMEOUT
    assert result.response is None


# --- the backstop, in a real turn ---------------------------------------


async def test_the_backstop_fires_when_the_classifier_fails(sessionmaker):
    """A classifier that is down must not mean the one case it exists
    for goes unanswered."""
    await _seed(sessionmaker, 1, intensity=3)
    bot, fake = _bot()
    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        FakeLLMProvider(text="Не оправдание. Что сделаешь за час?"),
        clock=_at(10, 0),
        chat_id=CHAT_ID,
        update_id=1,
        user_text="я больше не хочу жить",
        safety_provider=_Boom(),
    )

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.persona_active is False, "the persona must be off"
    assert fake.sent, "something must have been sent"
    assert "Не оправдание" not in fake.sent[-1].text, "the persona reply must be discarded"

    events = await _events(sessionmaker)
    assert [(e.kind, e.outcome) for e in events] == [("welfare", "fallback_hit")]


async def test_the_backstop_stays_quiet_on_ordinary_text(sessionmaker):
    """A failed classifier plus nothing alarming is still fail-open:
    the normal reply goes out, and the failure is recorded."""
    await _seed(sessionmaker, 2, intensity=3)
    bot, fake = _bot()
    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        FakeLLMProvider(text="Не оправдание. Что сделаешь за час?"),
        clock=_at(10, 0),
        chat_id=CHAT_ID,
        update_id=2,
        user_text="отчёт не готов, я устал",
        safety_provider=_Boom(),
    )

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.persona_active is True
    assert "Не оправдание" in fake.sent[-1].text

    events = await _events(sessionmaker)
    assert [(e.kind, e.outcome) for e in events] == [("welfare", "error")]


async def test_a_working_classifier_never_reaches_the_backstop(sessionmaker):
    """The backstop is a fallback, not a second opinion. A model that
    answered `none` about text containing a listed term is trusted --
    it saw context the keyword list cannot."""
    await _seed(sessionmaker, 3, intensity=3)
    bot, fake = _bot()
    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        FakeLLMProvider(text="Принято. Дальше."),
        clock=_at(10, 0),
        chat_id=CHAT_ID,
        update_id=3,
        # Contains a listed phrase, but in a plainly fictional frame.
        user_text="в книге герой решил покончить с собой, сильная сцена",
        safety_provider=FakeLLMProvider(text='{"level": "none", "confidence": 0.9}'),
    )

    async with sessionmaker() as session:
        state = await session.get(UserState, 1)
    assert state.persona_active is True
    assert "Принято" in fake.sent[-1].text

    events = await _events(sessionmaker)
    assert [(e.kind, e.outcome) for e in events] == [("welfare", "ok")]


async def test_a_skipped_check_records_nothing(sessionmaker):
    """No safety provider means no check ran -- which is not an outcome
    and must not be counted as one."""
    await _seed(sessionmaker, 4, intensity=3)
    bot, _ = _bot()
    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        FakeLLMProvider(text="Принято."),
        clock=_at(10, 0),
        chat_id=CHAT_ID,
        update_id=4,
        user_text="привет",
    )
    assert await _events(sessionmaker) == []


# --- the observability row ----------------------------------------------


def test_the_constants_match_the_database_constraints():
    """Both vocabularies are constrained in SQL, so a constant that
    drifts from the constraint would fail at INSERT time in production
    and nowhere else."""
    table = SafetyEvent.__table__
    constraints = {c.name: str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext")}
    kinds = constraints["ck_safety_event_kind"]
    outcomes = constraints["ck_safety_event_outcome"]
    for kind in safety_events.KINDS:
        assert f"'{kind}'" in kinds
    for outcome in welfare.OUTCOMES:
        assert f"'{outcome}'" in outcomes


async def test_recording_never_breaks_the_caller(sessionmaker):
    """Observability that can fail the turn it observes is a worse bug
    than the blindness it replaces."""
    await safety_events.record(
        sessionmaker,
        clock=_at(10, 0),
        timezone=TIMEZONE,
        kind="not-a-real-kind",  # violates ck_safety_event_kind
        outcome="ok",
    )
    assert await _events(sessionmaker) == []


async def test_counts_windows_on_local_days_and_ignores_fallback_hits(sessionmaker):
    clock = _at(10, 0)
    async with sessionmaker() as session:
        session.add_all(
            [
                SafetyEvent(local_date=DAY, kind="welfare", outcome="ok"),
                SafetyEvent(local_date=DAY, kind="welfare", outcome="ok"),
                SafetyEvent(local_date=DAY, kind="welfare", outcome="timeout"),
                SafetyEvent(local_date=DAY, kind="welfare", outcome="parse_fail"),
                SafetyEvent(local_date=DAY, kind="welfare", outcome="error"),
                # Neither ok nor a failure: the backstop worked.
                SafetyEvent(local_date=DAY, kind="welfare", outcome="fallback_hit"),
                # A different check, and a day outside the window.
                SafetyEvent(local_date=DAY, kind="tick", outcome="ok"),
                SafetyEvent(
                    local_date=DAY - datetime.timedelta(days=7),
                    kind="welfare",
                    outcome="ok",
                ),
            ]
        )
        await session.commit()

    async with sessionmaker() as session:
        ok, failures = await safety_events.counts(session, clock, TIMEZONE)
    assert (ok, failures) == (2, 3)

    async with sessionmaker() as session:
        edge = await safety_events.counts(session, clock, TIMEZONE, days=8)
    assert edge == (3, 3), "the 8-day window reaches one day further back"


# --- pricing -------------------------------------------------------------


def _triple(*prices) -> tuple:
    """price_triple_for returns Decimals built via Decimal(str(float)),
    so the expected side has to be built the same way -- comparing a
    Decimal to a float is what the function exists to avoid."""
    return tuple(decimal.Decimal(str(p)) for p in prices)


def test_the_safety_model_is_priced_as_itself():
    settings = _settings()
    assert price_triple_for(settings.LLM_MODEL_SAFETY, settings) == _triple(
        settings.LLM_SAFETY_PRICE_IN,
        settings.LLM_SAFETY_PRICE_CACHED,
        settings.LLM_SAFETY_PRICE_OUT,
    )


def test_an_unknown_model_still_falls_back_to_the_main_triple():
    """The conservative direction, unchanged by H2: the main model is the
    most expensive, so an unknown model is never under-billed against
    the daily cap."""
    settings = _settings()
    assert price_triple_for("someone/else", settings) == _triple(
        settings.LLM_PRICE_IN, settings.LLM_PRICE_CACHED, settings.LLM_PRICE_OUT
    )


def test_the_main_model_wins_when_two_settings_name_it():
    """The ordering rule price_triple_for's docstring relies on."""
    settings = _settings(LLM_MODEL_SAFETY="thedrummer/cydonia-24b-v4.1")
    assert price_triple_for(settings.LLM_MODEL, settings) == _triple(
        settings.LLM_PRICE_IN, settings.LLM_PRICE_CACHED, settings.LLM_PRICE_OUT
    )


def test_the_safety_model_defaults_off_the_persona_model():
    """The whole point: the classifier must not be the fine-tune it is
    meant to supervise."""
    settings = Settings(_env_file=None)
    assert settings.LLM_MODEL_SAFETY != settings.LLM_MODEL
    assert settings.LLM_SAFETY_TEMPERATURE == 0.0
