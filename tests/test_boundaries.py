"""The code-side medical/legal filter (H5).

The original plan asked for this boundary in "both code filters and
persona text". Only the persona text was built, so until H5 nothing
verified that the model listened.

Two halves are tested, and the second is the one that matters more:

- **It trips on advice.** A dose, an imperative to take something, a
  step-by-step for a filing, a predicted outcome.
- **It does not trip on the correct refusal.** «это к врачу» mentions a
  doctor. «сходи к юристу» mentions a lawyer. If the filter fired on
  those it would replace every right answer with a canned one, and the
  persona would lose the ability to refuse in its own voice -- which is
  the behaviour the whole boundary exists to produce.

The false-positive list is therefore longer than the true-positive one,
on purpose.
"""

from __future__ import annotations

import datetime

import pytest
from aiogram import Bot

from app.config import Settings
from app.core import boundaries, turn
from app.core.clock import FrozenClock, combine_local
from app.db.models import Message, SpendLedger, TelegramUpdate, UserState
from conftest import FakeLLMProvider, FakeSession

from sqlalchemy import select

TIMEZONE = "Europe/Paris"
CHAT_ID = 4242
DAY = datetime.date(2026, 9, 23)


def _settings(**overrides) -> Settings:
    base = dict(TZ_DEFAULT=TIMEZONE, DAILY_USD_CAP=10.0)
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _at(hour: int = 10, minute: int = 0) -> FrozenClock:
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


# --- the patterns --------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        # The dosage shape case 04 already encoded.
        "Мелатонин 3 мг за час до сна.",
        "Пей по 200 мг дважды в сутки.",
        "2 таблетки на ночь, не больше.",
        "Начни с 5мл и посмотри.",
        "Дозировку подбирай по весу.",
        "Принимай этот препарат курсом.",
        "Ибупрофен 400 мг снимет.",
        # Frequency of administration -- only in a medical frame; see
        # test_frequency_alone_is_not_medical below for the other half.
        "Три раза в день, по таблетке, после еды.",
        # The specific thing case 04 asks about.
        "Да, можно с алкоголем, ничего не будет.",
    ],
)
def test_medical_advice_trips_it(reply):
    assert boundaries.check(reply) == boundaries.MEDICAL


@pytest.mark.parametrize(
    "reply",
    [
        "Составь иск и укажи период невыплаты.",
        "Напиши жалобу в трудовую инспекцию по форме.",
        "Согласно статье 136 ТК он обязан платить дважды в месяц.",
        "Срок исковой давности по зарплате — три месяца.",
        "С такими доказательствами ты точно выиграешь.",
        "Суд обяжет его выплатить всё с процентами.",
        "В иске укажи сумму и приложи расчёт.",
    ],
)
def test_legal_advice_trips_it(reply):
    assert boundaries.check(reply) == boundaries.LEGAL


@pytest.mark.parametrize(
    "reply",
    [
        # The correct refusals. These are the replies the persona is
        # *supposed* to produce, and every one of them names a doctor, a
        # lawyer, a medicine or a court.
        "Тут я не советчик — это к врачу. Что у тебя главное на сегодня?",
        "Это к юристу, не ко мне. Один пункт на завтра — какой?",
        "Ты третью неделю не спишь. Это к врачу, а не ко мне.",
        "Врач скажет точнее. Давай про отчёт: что осталось?",
        "Юрист разберётся за час. Ты пока собери даты выплат.",
        "Я не буду советовать лекарства, извини.",
        "Позвони в трудовую инспекцию, они объяснят.",
        # Ordinary persona register that happens to share vocabulary.
        "Принимай как есть и двигайся дальше.",
        "Дай себе два дня.",
        "Ты обещал три пункта, сделал один.",
        "Закон подлости, но дедлайн не двигается.",
        "Выпей воды и вернись к отчёту.",
        "Час на это — и хватит.",
        "",
    ],
)
def test_the_right_answer_does_not_trip_it(reply):
    assert boundaries.check(reply) is None


def test_it_never_returns_the_matched_text():
    """Privacy: a category, never the fragment. Same discipline as
    app/core/welfare_terms.py."""
    result = boundaries.check("Пей по 200 мг дважды в сутки.")
    assert result in boundaries.CATEGORIES
    assert "200" not in result


@pytest.mark.parametrize(
    "reply",
    [
        # Frequency is a dosing shape only when something you take is in
        # the same reply. Bare frequency is this persona's native
        # register and must stay clean, or the filter would fire on
        # ordinary advice about habits.
        "Проверяй почту два раза в день.",
        "Созванивайтесь раз в неделю.",
        "Пиши мне три раза в день, если буксуешь.",
        "Отчёт — раз в день, не чаще.",
    ],
)
def test_frequency_alone_is_not_medical(reply):
    assert boundaries.check(reply) is None


def test_medical_wins_when_a_reply_trips_both():
    """A wrong dose is the more immediate harm, and the reply is being
    replaced either way."""
    assert boundaries.check("Пей 200 мг и составь иск.") == boundaries.MEDICAL


def test_e_folding_matches_the_way_the_terms_are_written():
    assert boundaries.check("Срок исковой давности — три месяца.") == boundaries.LEGAL


# --- the hook ------------------------------------------------------------


class _ScriptedReplies(FakeLLMProvider):
    """Answers with a different text on each successive call."""

    def __init__(self, *texts: str, **kw):
        super().__init__(**kw)
        self._texts = list(texts)

    async def complete(self, messages, **kw):
        response = await super().complete(messages, **kw)
        text = self._texts.pop(0) if self._texts else self.text
        return type(response)(text=text, usage=response.usage, model=response.model)


async def test_one_regeneration_rescues_a_tripped_reply(sessionmaker):
    """The happy path: the model corrects itself and the user never sees
    that anything happened."""
    await _seed(sessionmaker, 1, intensity=3)
    bot, fake = _bot()
    provider = _ScriptedReplies(
        "Мелатонин 3 мг за час до сна.",
        "Это к врачу, не ко мне. Что сегодня главное?",
    )
    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        provider,
        clock=_at(),
        chat_id=CHAT_ID,
        update_id=1,
        user_text="сколько мелатонина пить?",
    )

    assert provider.calls == 2, "exactly one retry"
    assert len(fake.sent) == 1
    assert "3 мг" not in fake.sent[0].text
    assert "к врачу" in fake.sent[0].text


async def test_a_second_trip_sends_the_canned_refusal(sessionmaker):
    """Twice is enough. A model that ignored an explicit correction will
    not comply on the third attempt."""
    await _seed(sessionmaker, 2, intensity=3)
    bot, fake = _bot()
    provider = _ScriptedReplies(
        "Мелатонин 3 мг за час до сна.",
        "Ладно: 5 мг, и можно с алкоголем.",
    )
    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        provider,
        clock=_at(),
        chat_id=CHAT_ID,
        update_id=2,
        user_text="сколько мелатонина пить?",
    )

    assert provider.calls == 2, "no third attempt"
    assert len(fake.sent) == 1
    assert fake.sent[0].text == boundaries.REFUSAL_REPLY_TEXT


async def test_both_discarded_generations_are_still_ledgered(sessionmaker):
    """They were billed, so they are recorded -- the rule the welfare
    path already follows for the reply it throws away."""
    await _seed(sessionmaker, 3, intensity=3)
    bot, _ = _bot()
    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        _ScriptedReplies("Пей 200 мг.", "Прими 400 мг."),
        clock=_at(),
        chat_id=CHAT_ID,
        update_id=3,
        user_text="что выпить?",
    )

    async with sessionmaker() as session:
        rows = (await session.execute(select(SpendLedger))).scalars().all()
    assert len(rows) == 2, "one row per billed call, neither of them sent"


async def test_the_tripped_text_is_never_stored(sessionmaker):
    """A stored reply can be replayed, summarized, or fed back into the
    transcript. The discarded one must not exist anywhere."""
    await _seed(sessionmaker, 4, intensity=3)
    bot, _ = _bot()
    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        _ScriptedReplies("Мелатонин 3 мг за час до сна.", "Это к врачу."),
        clock=_at(),
        chat_id=CHAT_ID,
        update_id=4,
        user_text="сколько пить?",
    )

    async with sessionmaker() as session:
        rows = (await session.execute(select(Message))).scalars().all()
    assert not any("3 мг" in (row.content or "") for row in rows)


async def test_a_clean_reply_costs_no_extra_call(sessionmaker):
    """The filter is a tripwire, not a step in the pipeline."""
    await _seed(sessionmaker, 5, intensity=3)
    bot, fake = _bot()
    provider = FakeLLMProvider(text="Это к врачу. Что сегодня главное?")
    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        provider,
        clock=_at(),
        chat_id=CHAT_ID,
        update_id=5,
        user_text="не сплю",
    )

    assert provider.calls == 1
    assert fake.sent[0].text == "Это к врачу. Что сегодня главное?"


async def test_neutral_mode_does_not_regenerate(sessionmaker):
    """Persona off means there is no persona instruction to reinforce, so
    a retry would spend money to say the same thing again."""
    await _seed(sessionmaker, 6, intensity=3, persona_active=False)
    bot, _ = _bot()
    provider = _ScriptedReplies("Мелатонин 3 мг за час до сна.", "второй")
    await turn.run(
        sessionmaker,
        bot,
        _settings(),
        provider,
        clock=_at(),
        chat_id=CHAT_ID,
        update_id=6,
        user_text="сколько пить?",
    )

    assert provider.calls == 1
