"""app/core/mood.py (phase-5 plan section 4).

`mood()` itself needs no database and no clock of its own -- it is
tested here as the pure function it is, against a lightweight stand-in
for `UserState` that carries only the three attributes it reads
(`welfare_at`, `intensity`, `streak`). `load_mood_facts()` is the one
part that touches the database and gets its own section below, against
the real `sessionmaker`/`clock` fixtures.
"""

from __future__ import annotations

import dataclasses
import datetime
import itertools

import pytest

from app.core.mood import GLOSS, MOODS, MoodFacts, load_mood_facts, mood
from app.db.models import Checkin, Message, TelegramUpdate, UserState

NOW = datetime.datetime(2026, 9, 22, 12, 0, tzinfo=datetime.timezone.utc)


@dataclasses.dataclass
class _State:
    """Everything `mood()` reads off `UserState`, nothing else."""

    welfare_at: datetime.datetime | None = None
    intensity: int = 3
    streak: int = 0


def _state(**overrides) -> _State:
    return _State(**overrides)


def _facts(**overrides) -> MoodFacts:
    return MoodFacts(**overrides)


# --- MOODS / GLOSS -------------------------------------------------------


def test_moods_is_exactly_the_plans_four_values():
    assert MOODS == ("доволен", "ровный", "настороже", "ждёт")


def test_gloss_covers_every_mood_with_the_plans_text():
    assert set(GLOSS) == set(MOODS)
    assert GLOSS["доволен"] == "теплее, коротко похвали по делу"
    assert GLOSS["ровный"] == "спокойно"
    assert GLOSS["настороже"] == "собранно, без упрёков"
    assert GLOSS["ждёт"] == "спокойно, без давления"


def test_no_mood_is_punitive():
    """No "angry", "disappointed" or "strict" value exists at all --
    настороже is the sharpest this gets, and its own gloss ("без
    упрёков") tells the model not to use it as license to scold."""
    forbidden = ("зол", "серди", "разочарован", "строг", "наказ")
    for value in MOODS:
        for word in forbidden:
            assert word not in value

    for gloss in GLOSS.values():
        for word in forbidden:
            assert word not in gloss


# --- rule 1: welfare / low intensity forces ровный ------------------------


def test_recent_welfare_forces_rovny_even_with_a_dovolen_streak():
    state = _state(welfare_at=NOW - datetime.timedelta(hours=1), streak=5)
    facts = _facts(due_results=("done",))
    assert mood(state, facts, NOW) == "ровный"


def test_welfare_at_exactly_24h_no_longer_forces_it():
    """"Within 24h" is a strict boundary: at exactly 24h the trigger no
    longer applies, and whatever the later rules say wins instead."""
    state = _state(welfare_at=NOW - datetime.timedelta(hours=24), streak=5)
    facts = _facts(due_results=("done",))
    assert mood(state, facts, NOW) == "доволен"


def test_welfare_just_under_24h_still_forces_it():
    state = _state(welfare_at=NOW - datetime.timedelta(hours=23, minutes=59), streak=5)
    facts = _facts(due_results=("done",))
    assert mood(state, facts, NOW) == "ровный"


@pytest.mark.parametrize("intensity", [1, 2])
def test_low_intensity_forces_rovny(intensity):
    state = _state(intensity=intensity, streak=5)
    facts = _facts(due_results=("done",))
    assert mood(state, facts, NOW) == "ровный"


@pytest.mark.parametrize("intensity", [3, 4, 5])
def test_higher_intensity_does_not_force_it(intensity):
    state = _state(intensity=intensity, streak=5)
    facts = _facts(due_results=("done",))
    assert mood(state, facts, NOW) == "доволен"


# --- rule 2: доволен -------------------------------------------------------


def test_a_real_streak_with_yesterdays_done_is_dovolen():
    state = _state(streak=3)
    facts = _facts(due_results=("done", "partial"))
    assert mood(state, facts, NOW) == "доволен"


def test_streak_below_three_is_not_dovolen():
    state = _state(streak=2)
    facts = _facts(due_results=("done",))
    assert mood(state, facts, NOW) != "доволен"


def test_a_streak_with_no_done_result_is_not_dovolen():
    state = _state(streak=5)
    facts = _facts(due_results=("partial",))
    assert mood(state, facts, NOW) != "доволен"


def test_a_streak_with_no_due_results_at_all_is_not_dovolen():
    state = _state(streak=5)
    facts = _facts(due_results=())
    assert mood(state, facts, NOW) != "доволен"


# --- rule 3: настороже ------------------------------------------------------


def test_missed_evening_checkin_is_nastorozhe():
    state = _state(streak=0)
    facts = _facts(missed_evening_yesterday=True)
    assert mood(state, facts, NOW) == "настороже"


def test_two_misses_in_a_row_is_nastorozhe():
    state = _state(streak=0)
    facts = _facts(due_results=("no", "partial"))
    assert mood(state, facts, NOW) == "настороже"


@pytest.mark.parametrize("results", [("done", "no"), ("no", "done"), ("done",), ("no",)])
def test_a_single_miss_is_not_enough(results):
    state = _state(streak=0)
    facts = _facts(due_results=results)
    assert mood(state, facts, NOW) != "настороже"


def test_missed_evening_or_two_misses_is_an_or_not_an_and():
    """Neither implies the other, so either alone must trigger настороже."""
    state = _state(streak=0)
    only_missed_evening = _facts(missed_evening_yesterday=True, due_results=("done", "done"))
    assert mood(state, only_missed_evening, NOW) == "настороже"

    only_two_misses = _facts(missed_evening_yesterday=False, due_results=("no", "no"))
    assert mood(state, only_two_misses, NOW) == "настороже"


# --- rule 4: ждёт ------------------------------------------------------------


def test_no_user_message_ever_is_zhdyot():
    state = _state(streak=0)
    facts = _facts(last_user_msg_before_now=None)
    assert mood(state, facts, NOW) == "ждёт"


def test_silence_just_under_24h_is_not_zhdyot():
    state = _state(streak=0)
    facts = _facts(last_user_msg_before_now=NOW - datetime.timedelta(hours=23, minutes=59))
    assert mood(state, facts, NOW) == "ровный"


def test_silence_at_exactly_24h_is_zhdyot():
    state = _state(streak=0)
    facts = _facts(last_user_msg_before_now=NOW - datetime.timedelta(hours=24))
    assert mood(state, facts, NOW) == "ждёт"


# --- rule 5: fallback --------------------------------------------------------


def test_the_fallback_is_rovny():
    state = _state(streak=0)
    facts = _facts(last_user_msg_before_now=NOW - datetime.timedelta(hours=1))
    assert mood(state, facts, NOW) == "ровный"


# --- precedence --------------------------------------------------------------


def test_rule_one_beats_two_three_and_four():
    state = _state(welfare_at=NOW - datetime.timedelta(minutes=5), intensity=4, streak=5)
    facts = _facts(
        due_results=("done",),  # would be rule 2
        missed_evening_yesterday=True,  # would be rule 3
        last_user_msg_before_now=None,  # would be rule 4
    )
    assert mood(state, facts, NOW) == "ровный"


def test_rule_two_beats_three():
    state = _state(streak=3)
    facts = _facts(due_results=("done",), missed_evening_yesterday=True)
    assert mood(state, facts, NOW) == "доволен"


def test_rule_three_beats_four():
    state = _state(streak=0)
    facts = _facts(missed_evening_yesterday=True, last_user_msg_before_now=None)
    assert mood(state, facts, NOW) == "настороже"


# --- exhaustive sweep ---------------------------------------------------------


def test_every_combination_stays_in_moods_and_never_punitive():
    """A property-style sweep, not a fuzzer: the state space is small
    enough to walk exactly."""
    welfare_options = (None, NOW - datetime.timedelta(hours=1), NOW - datetime.timedelta(hours=48))
    intensity_options = (1, 3, 5)
    streak_options = (0, 3)
    due_options = ((), ("done",), ("no", "no"), ("done", "partial"))
    missed_options = (False, True)
    last_msg_options = (None, NOW - datetime.timedelta(hours=1), NOW - datetime.timedelta(hours=48))

    for welfare_at, intensity, streak, due, missed, last_msg in itertools.product(
        welfare_options, intensity_options, streak_options, due_options, missed_options, last_msg_options
    ):
        state = _state(welfare_at=welfare_at, intensity=intensity, streak=streak)
        facts = _facts(
            due_results=due, missed_evening_yesterday=missed, last_user_msg_before_now=last_msg
        )
        result = mood(state, facts, NOW)
        assert result in MOODS


# --- load_mood_facts (DB) -----------------------------------------------------

TIMEZONE = "Europe/Paris"


async def _seed_state(sessionmaker, **overrides) -> UserState:
    async with sessionmaker() as session:
        state = UserState(id=1, chat_id=1, timezone=TIMEZONE, **overrides)
        session.add(state)
        await session.commit()
        await session.refresh(state)
        return state


async def _add_checkin(sessionmaker, local_date, due_result=None) -> None:
    async with sessionmaker() as session:
        session.add(Checkin(local_date=local_date, due_result=due_result))
        await session.commit()


async def _add_user_message(sessionmaker, *, update_id: int, created_at=None) -> None:
    async with sessionmaker() as session:
        session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()
        row = Message(role="user", content="привет", update_id=update_id)
        session.add(row)
        await session.commit()
        if created_at is not None:
            row.created_at = created_at
            await session.commit()


async def test_due_results_are_newest_first_capped_at_two_and_ignore_none(
    sessionmaker, frozen_clock
):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TIMEZONE)
    state = await _seed_state(sessionmaker)
    from app.core import clock as clock_module

    local_today = clock_module.local_date(clock, TIMEZONE)
    await _add_checkin(sessionmaker, local_today - datetime.timedelta(days=1), "done")
    await _add_checkin(sessionmaker, local_today - datetime.timedelta(days=2), "no")
    await _add_checkin(sessionmaker, local_today - datetime.timedelta(days=3), "none")
    await _add_checkin(sessionmaker, local_today - datetime.timedelta(days=4), None)
    await _add_checkin(sessionmaker, local_today - datetime.timedelta(days=5), "partial")

    async with sessionmaker() as session:
        facts = await load_mood_facts(session, state, clock)

    # Newest first, 'none' and NULL skipped, capped at 2.
    assert facts.due_results == ("done", "no")


async def test_missed_evening_yesterday_true_when_no_row_and_an_earlier_one_exists(
    sessionmaker, frozen_clock
):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TIMEZONE)
    state = await _seed_state(sessionmaker)
    from app.core import clock as clock_module

    local_today = clock_module.local_date(clock, TIMEZONE)
    # No row for yesterday, but one for the day before -- history exists.
    await _add_checkin(sessionmaker, local_today - datetime.timedelta(days=2), "done")

    async with sessionmaker() as session:
        facts = await load_mood_facts(session, state, clock)

    assert facts.missed_evening_yesterday is True


async def test_a_first_day_user_with_no_history_does_not_count_as_missed(
    sessionmaker, frozen_clock
):
    """No check-in at all yet -- not even an earlier one -- must not
    read as "missed yesterday"."""
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TIMEZONE)
    state = await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        facts = await load_mood_facts(session, state, clock)

    assert facts.missed_evening_yesterday is False


async def test_yesterdays_checkin_present_means_not_missed(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TIMEZONE)
    state = await _seed_state(sessionmaker)
    from app.core import clock as clock_module

    local_today = clock_module.local_date(clock, TIMEZONE)
    await _add_checkin(sessionmaker, local_today - datetime.timedelta(days=1), "done")
    await _add_checkin(sessionmaker, local_today - datetime.timedelta(days=2), "done")

    async with sessionmaker() as session:
        facts = await load_mood_facts(session, state, clock)

    assert facts.missed_evening_yesterday is False


async def test_last_user_message_excludes_the_current_turns_own_row(
    sessionmaker, frozen_clock
):
    """The current turn's user row was already stored (app/core/turn.py
    step 1) by the time load_mood_facts runs -- it must not count as
    "the last message" for its own turn."""
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TIMEZONE)
    state = await _seed_state(sessionmaker)

    earlier = clock.now_utc() - datetime.timedelta(hours=5)
    now_stamp = clock.now_utc()
    await _add_user_message(sessionmaker, update_id=1, created_at=earlier)
    await _add_user_message(sessionmaker, update_id=2, created_at=now_stamp)

    async with sessionmaker() as session:
        # This turn's own message is update_id=2: excluding it must
        # surface update_id=1's (earlier) timestamp instead.
        facts_excluding = await load_mood_facts(session, state, clock, exclude_update_id=2)
        # With nothing excluded (the outbound path), the newest row wins.
        facts_including_all = await load_mood_facts(session, state, clock)

    assert facts_excluding.last_user_msg_before_now == earlier
    assert facts_including_all.last_user_msg_before_now == now_stamp


async def test_no_user_message_at_all_gives_none(sessionmaker, frozen_clock):
    clock = frozen_clock(2026, 9, 22, 12, 0, tz=TIMEZONE)
    state = await _seed_state(sessionmaker)

    async with sessionmaker() as session:
        facts = await load_mood_facts(session, state, clock)

    assert facts.last_user_msg_before_now is None
