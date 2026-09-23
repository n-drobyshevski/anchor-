"""The risk rules, in both directions (phase-4 plan sections 8 and 14).

Plan section 14 asks for "each rule id has positive and negative
examples", and the negative half is the one that matters. A risk list
only ever gets tested for the thing it catches, so it drifts wider every
time someone adds a term, until it hides good cards and the user stops
reading /notes. The `CLEAN` table below is the counterweight: every
entry is a card this bot should actually want, several of them one word
away from a rule.

`test_every_rule_id_has_both_kinds_of_example` fails if a new rule is
added without both, so the table cannot fall behind the module.
"""

from __future__ import annotations

import pytest

from app.research import risk

# (rule id, text that must hit it). One or more per rule, across the
# three languages the bot deals with.
HITS: tuple[tuple[str, str], ...] = (
    ("health_meds", "Принимайте 500 мг магния за час до сна."),
    ("health_meds", "Дозировка подбирается индивидуально."),
    ("health_meds", "Мелатонин помогает при смене часовых поясов."),
    ("health_meds", "Take a melatonin supplement, 3 mg, nightly."),
    ("health_meds", "Ask about the dosage of your prescription."),
    ("body_permanent", "Сделайте татуировку с напоминанием на запястье."),
    ("body_permanent", "Пирсинг как способ отметить достижение."),
    ("body_permanent", "This change to your body is irreversible."),
    ("body_permanent", "Consider surgery if nothing else works."),
    ("self_harm", "Иногда хочется покончить с собой."),
    ("self_harm", "I want to kill myself."),
    ("extreme_restriction", "Сутки без еды раз в неделю очищают голову."),
    ("extreme_restriction", "Голодание по 36 часов."),
    ("extreme_restriction", "Не спать всю ночь перед дедлайном."),
    ("extreme_restriction", "Держите меньше 800 ккал в день."),
    ("extreme_restriction", "Try fasting for two days."),
    ("extreme_restriction", "Sleep deprivation sharpens focus."),
    # 5c: meal-skipping shapes (implementation plan's Decisions,
    # "Risk rule") -- standing orders are screened regardless of
    # author, so the same rule that hides a full fast has to hide
    # "don't eat until evening" too.
    ("extreme_restriction", "Не есть до вечера."),
    ("extreme_restriction", "Не есть до обеда, потом можно всё."),
    ("extreme_restriction", "Без еды до полудня."),
    ("extreme_restriction", "Пропускать завтрак каждый день."),
    ("extreme_restriction", "Не завтракать вовсе."),
    ("extreme_restriction", "Не ужинать совсем."),
    ("illegal", "Наркотики помогают сосредоточиться."),
    ("illegal", "Проще украсть, чем покупать."),
    ("illegal", "You can hack into the system to check."),
    ("illegal", "Learn to evade the police."),
    ("third_party", "Следить за ним через геолокацию."),
    ("third_party", "Надавить на неё, если отказывается."),
    ("third_party", "Сделайте это без его согласия."),
    ("third_party", "Track them without their knowledge."),
    ("third_party", "Blackmail is effective."),
    ("physical_devices", "Замок с таймером на холодильник."),
    ("physical_devices", "Запереть телефон до утра."),
    ("physical_devices", "Use a timer lock box for your phone."),
    ("financial", "Вложить деньги в криптовалюту."),
    ("financial", "Держите портфель из индексных фондов."),
    ("financial", "Wire transfer the amount every month."),
    ("intensity", "Будь строже к себе, никаких исключений."),
    ("intensity", "Наказание за пропущенный день."),
    ("intensity", "Be harsher when you slip."),
    ("intensity", "Escalate the penalty for each miss."),
)

# Cards this bot should actually want. Several are one word away from a
# rule, and that is the point: they are the regression test for the next
# person who widens a pattern.
CLEAN: tuple[tuple[str, str], ...] = (
    ("health_meds", "Ложитесь спать в одно и то же время каждый день."),
    ("health_meds", "Выпейте стакан воды сразу после пробуждения."),
    ("body_permanent", "Оперативно отвечайте на письма один раз в день."),
    ("body_permanent", "Отметьте выполненное дело галочкой в списке."),
    ("self_harm", "Убейте время в очереди чтением, а не лентой."),
    ("extreme_restriction", "Не есть за три часа до сна."),
    ("extreme_restriction", "Не спать днём дольше двадцати минут."),
    ("extreme_restriction", "Спите восемь часов, ложась до полуночи."),
    ("extreme_restriction", "Работайте 25 минут, потом 5 минут отдыха."),
    # 5c: the meal-skipping widening must not catch these -- neither has
    # the "не есть до <часть дня>" / "пропускать <приём пищи>" /
    # "не завтракать|обедать|ужинать" shape the new patterns require.
    ("extreme_restriction", "Не есть сладкое после шести."),
    ("extreme_restriction", "Не есть за три часа до сна для лучшего сна."),
    # A cut-off time is ordinary advice, not a skipped meal.
    ("extreme_restriction", "Не ужинать после девяти вечера."),
    ("extreme_restriction", "Не обедать позже трёх."),
    ("illegal", "Уберите телефон в другую комнату на вечер."),
    ("third_party", "Контролировать своё время помогает список дел."),
    ("third_party", "Заставить себя начать проще, чем закончить."),
    ("physical_devices", "Делайте перерыв каждые 25 минут по таймеру."),
    ("physical_devices", "Поставьте таймер на десять минут и начните."),
    ("financial", "Акция в магазине — не повод покупать лишнее."),
    ("financial", "Ведите бюджет: записывайте расходы каждый вечер."),
    ("intensity", "Строение дня важнее силы воли."),
    ("intensity", "Начните с самого лёгкого шага, а не с трудного."),
)


@pytest.mark.parametrize("rule_id,text", HITS, ids=[f"{r}-{i}" for i, (r, _) in enumerate(HITS)])
def test_a_card_that_should_be_flagged_is(rule_id, text):
    level, hits = risk.assess(text)
    assert rule_id in hits, f"{rule_id} missed: {text!r} -> {hits}"
    assert level in (risk.MEDIUM, risk.HIGH)


@pytest.mark.parametrize("rule_id,text", CLEAN, ids=[f"{r}-{i}" for i, (r, _) in enumerate(CLEAN)])
def test_an_ordinary_technique_is_not_flagged(rule_id, text):
    """Each of these is near the named rule and must stay clean.

    A false positive here is not a cosmetic problem: the card is hidden
    or shown as risky, the user learns /notes is noisy, and the filter
    they stop reading is worse than the one that was merely narrow.
    """
    level, hits = risk.assess(text)
    assert hits == [], f"{rule_id} false positive on {text!r}"
    assert level == risk.LOW


def test_every_rule_id_has_both_kinds_of_example():
    """A new rule without a negative example is a rule nobody measured."""
    with_hits = {rule_id for rule_id, _ in HITS}
    with_clean = {rule_id for rule_id, _ in CLEAN}
    assert with_hits == set(risk.RULE_IDS), f"no positive example: {set(risk.RULE_IDS) - with_hits}"
    assert with_clean == set(risk.RULE_IDS), f"no negative example: {set(risk.RULE_IDS) - with_clean}"


def test_rules_never_lower_a_model_risk():
    """Plan section 8: rules only ever raise. The model saying `high`
    about something the rules have no opinion on stays `high`."""
    level, hits = risk.assess("Ложитесь спать в одно и то же время каждый день.")
    assert (level, hits) == (risk.LOW, [])
    assert risk.max_level("high", level) == risk.HIGH
    assert risk.max_level("medium", level) == risk.MEDIUM


@pytest.mark.parametrize(
    "levels,expected",
    [
        (("low", "low"), "low"),
        (("low", "medium"), "medium"),
        (("medium", "low"), "medium"),
        (("low", "high"), "high"),
        (("high", "low"), "high"),
        (("medium", "high"), "high"),
    ],
)
def test_max_level_is_the_maximum(levels, expected):
    assert risk.max_level(*levels) == expected


@pytest.mark.parametrize("bogus", ["", "unknown", "LOW", "critical", "safe"])
def test_an_unrecognised_level_reads_as_high(bogus):
    """The model can emit a label outside the enum. "I do not recognise
    this risk label" does not mean "therefore it is low"."""
    assert risk.max_level("low", bogus) == risk.HIGH


def test_the_quote_is_assessed_as_well_as_the_text():
    """A quote naming a dosage makes the card a dosage card, however
    carefully the paraphrase avoided saying so."""
    level, hits = risk.assess(
        "Минерал перед сном помогает заснуть.", "Принимайте 500 мг магния перед сном."
    )
    assert level == risk.HIGH
    assert hits == ["health_meds"]


def test_hits_are_ordered_highest_first():
    level, hits = risk.assess("Будь строже: 500 мг магния и сутки без еды.")
    assert level == risk.HIGH
    assert hits[-1] == "intensity", "the medium rule sorts after the high ones"
    assert set(hits[:-1]) == {"health_meds", "extreme_restriction"}


def test_assess_returns_ids_never_matched_text():
    """`rule_hits` is a column /export dumps, and the matched text came
    from a fetched page."""
    _, hits = risk.assess("Принимайте 500 мг магния перед сном.")
    assert hits == ["health_meds"]
    for rule_id in hits:
        assert rule_id in risk.RULE_IDS


def test_none_and_empty_inputs_are_ignored():
    assert risk.assess(None, "", None) == (risk.LOW, [])


def test_yo_is_folded():
    """«жёстче» and «жестче» are the same word for this purpose."""
    assert risk.assess("Будь жёстче.")[0] == risk.MEDIUM
    assert risk.assess("Будь жестче.")[0] == risk.MEDIUM
