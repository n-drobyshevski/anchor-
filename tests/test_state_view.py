"""app/tg/state_view.py: the rich /state view, and its router wiring.

Two kinds of test here. The first three groups are pure -- no DB, no
clock of its own, just state_view.render()/refresh_keyboard() called
directly against hand-built inputs, mirroring how app/tg/router.py's
_format_state is already tested indirectly through tests/
test_state_commands.py. The last group ("router") goes through the real
Dispatcher, the same way tests/test_state_commands.py's /state tests do,
because it is exercising router.py's own wiring (which bot method gets
called, the TelegramBadRequest fallback, the refresh callback) rather
than state_view's own logic.

The parity group is the drift guard the plan asked for: every value
_format_state would print also shows up somewhere in the rich view's
flattened text. "Локальное время" is the one deliberate exception --
state_view drops that live snapshot entirely (see its own comment and
docs/decisions.md) in favour of the footer's live "Обновлено" timestamp.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendRichMessage, TelegramMethod
from aiogram.types import Update

from app.config import Settings
from app.core.clock import FrozenClock
from app.core.outbound import StateSummary
from app.db.models import TelegramUpdate, UserState
from app.tg import state_view
from app.tg.router import _format_state, build_router
from conftest import FakeLLMProvider, FakeSession, flatten_rich_message, make_bot

pytestmark = pytest.mark.asyncio

TIMEZONE = "Europe/Paris"
CHAT_ID = 555
NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc)


def _user_state(**overrides) -> UserState:
    base = dict(
        id=1,
        chat_id=CHAT_ID,
        timezone=TIMEZONE,
        persona_active=True,
        intensity=3,
        focus_on=False,
        streak=0,
        last_checkin_at=None,
        due_action=None,
        due_set_at=None,
        attention="present",
        attention_until=None,
    )
    base.update(overrides)
    return UserState(**base)


def _kw(**overrides) -> dict:
    """render()/_format_state's shared kwargs, defaulted to "nothing to
    show" so a fixture only has to name what it actually wants."""
    base = dict(
        user_state=_user_state(),
        spend=decimal.Decimal("0.00"),
        settings=Settings(_env_file=None),
        clock=FrozenClock(NOW),
        by_category=None,
        memories=0,
        outbound=None,
        welfare_counts=None,
        research_counts=None,
        mood="ровный",
        idle=None,
        canary=None,
        backup=None,
        debts=None,
        persona_version=None,
        vault_line=None,
    )
    base.update(overrides)
    return base


# --- (a) block order -----------------------------------------------------


def test_render_produces_heading_table_heading_table_details_footer_in_order():
    msg = state_view.render(**_kw())
    kinds = [block.type.value for block in msg.blocks]
    assert kinds == ["heading", "table", "heading", "table", "details", "footer"]


def test_the_two_headings_are_state_and_today():
    msg = state_view.render(**_kw())
    heading_texts = [block.text for block in msg.blocks if block.type.value == "heading"]
    assert heading_texts == ["Состояние", "Сегодня"]


def test_the_details_block_is_closed_and_named_system():
    msg = state_view.render(**_kw())
    details = next(b for b in msg.blocks if b.type.value == "details")
    assert details.summary == "Система"
    assert details.is_open is False


# --- (b) parity with _format_state ---------------------------------------


def _parity_fragments(plain_text: str) -> list[str]:
    """Every "part after the first ': '" _format_state's lines carry,
    decomposed further on " · " so a line combining two labels (e.g.
    "Интенсивность: 4/5 · Фокус: вкл") yields the same two fragments the
    rich view shows as two separate rows, rather than one continuous
    string that would never appear intact once decomposed into rows.
    "Локальное время" is the one documented drop -- see this module's
    own docstring.
    """
    fragments = []
    for line in plain_text.splitlines():
        if not line or line.startswith("Локальное время"):
            continue
        for segment in line.split(" · "):
            fragments.append(segment.split(": ", 1)[1] if ": " in segment else segment)
    return [f for f in fragments if f]


FRESH = _kw()

WITH_DUE_DEBT_ATTENTION = _kw(
    user_state=_user_state(
        due_action="сдать отчёт",
        due_set_at=NOW - datetime.timedelta(days=2),
        last_checkin_at=NOW - datetime.timedelta(days=1),
        streak=6,
        intensity=4,
        focus_on=True,
        attention="short",
        attention_until=NOW + datetime.timedelta(hours=1),
    ),
    spend=decimal.Decimal("0.22"),
    by_category={"chat": decimal.Decimal("0.02"), "extractor": decimal.Decimal("0.01")},
    memories=3,
    debts=(2, 1),
    persona_version="abcd1234",
    vault_line="Хранилище: ок",
)

WITH_OUTBOUND = _kw(
    outbound=StateSummary(
        sent_today=1,
        max_per_day=3,
        ignored_in_row=2,
        quiet_until=NOW + datetime.timedelta(hours=2),
        next_kind="evening_nag",
        next_planned_for=NOW + datetime.timedelta(hours=5),
        last_skip_reason="quiet",
    ),
    welfare_counts=(3, 2),
    research_counts=((3, 2), (1, 4)),
)

WITH_BACKUP_CANARY_IDLE = _kw(
    backup=(NOW, "ok"),
    canary=(datetime.date(2026, 9, 23), True),
    idle=(0.5, 1.0, 2),
)

WITH_FAILED_BACKUP_AND_STOPPED_VAULT = _kw(
    backup=(NOW, "failed"),
    canary=(datetime.date(2026, 9, 23), False),
    vault_line="Хранилище: синхронизация остановлена",
)

FIXTURES = {
    "fresh": FRESH,
    "due_debt_attention": WITH_DUE_DEBT_ATTENTION,
    "outbound": WITH_OUTBOUND,
    "backup_canary_idle": WITH_BACKUP_CANARY_IDLE,
    "failed_backup_stopped_vault": WITH_FAILED_BACKUP_AND_STOPPED_VAULT,
}


@pytest.mark.parametrize("name", list(FIXTURES))
def test_every_format_state_value_appears_in_the_rich_view(name):
    kw = FIXTURES[name]
    plain = _format_state(**kw)
    rich_text = flatten_rich_message(state_view.render(**kw))
    for fragment in _parity_fragments(plain):
        assert fragment in rich_text, f"{fragment!r} missing from rich /state ({name})"


# --- (c) date_time entities ------------------------------------------------


def _collect_date_time_entities(msg) -> list[dict]:
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "date_time":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(msg.model_dump(mode="json", exclude_none=True))
    return found


DATE_TIME_FORMAT_RE = r"^(r|w?[dD]?[tT]?)$"


def test_date_time_entities_have_well_formed_formats_and_unix_times():
    import re

    for kw in (WITH_DUE_DEBT_ATTENTION, WITH_OUTBOUND, WITH_BACKUP_CANARY_IDLE):
        msg = state_view.render(**kw)
        entities = _collect_date_time_entities(msg)
        assert entities, "expected at least one date_time entity"
        for entity in entities:
            assert re.match(DATE_TIME_FORMAT_RE, entity["date_time_format"])
            assert isinstance(entity["unix_time"], int)


def test_last_checkin_is_relative_and_backup_is_date_time():
    kw = WITH_DUE_DEBT_ATTENTION
    msg = state_view.render(**kw)
    entities = _collect_date_time_entities(msg)
    formats = {e["date_time_format"] for e in entities}
    assert "r" in formats  # last check-in
    assert "t" in formats  # "коротко до"

    msg2 = state_view.render(**WITH_BACKUP_CANARY_IDLE)
    entities2 = _collect_date_time_entities(msg2)
    backup_entity = next(e for e in entities2 if e["unix_time"] == int(NOW.timestamp()))
    assert backup_entity["date_time_format"] == "dt"


def test_quiet_until_and_next_outbound_use_the_documented_formats():
    msg = state_view.render(**WITH_OUTBOUND)
    entities = _collect_date_time_entities(msg)
    summary = WITH_OUTBOUND["outbound"]
    quiet_entity = next(e for e in entities if e["unix_time"] == int(summary.quiet_until.timestamp()))
    assert quiet_entity["date_time_format"] == "dt"
    next_entity = next(
        e for e in entities if e["unix_time"] == int(summary.next_planned_for.timestamp())
    )
    assert next_entity["date_time_format"] == "t"


def test_footer_carries_a_relative_now_entity():
    msg = state_view.render(**FRESH)
    footer = msg.blocks[-1]
    assert footer.type.value == "footer"
    entities = _collect_date_time_entities(footer)
    assert len(entities) == 1
    assert entities[0]["date_time_format"] == "r"
    assert entities[0]["unix_time"] == int(NOW.timestamp())


# --- (d) every table cell has a valid align/valign -------------------------


def _collect_cells(msg) -> list[dict]:
    cells = []

    def walk_block(block: dict) -> None:
        if block["type"] == "table":
            for row in block["cells"]:
                cells.extend(row)
        elif block["type"] == "details":
            for inner in block["blocks"]:
                walk_block(inner)

    for block in msg.model_dump(mode="json", exclude_none=True)["blocks"]:
        walk_block(block)
    return cells


@pytest.mark.parametrize("name", list(FIXTURES))
def test_every_table_cell_has_a_valid_align_and_valign(name):
    cells = _collect_cells(state_view.render(**FIXTURES[name]))
    assert cells
    for cell in cells:
        assert cell["align"] in ("left", "center", "right")
        assert cell["valign"] in ("top", "middle", "bottom")


# --- refresh_keyboard ------------------------------------------------------


def test_refresh_keyboard_has_one_button_with_the_st_r_callback():
    markup = state_view.refresh_keyboard()
    assert len(markup.inline_keyboard) == 1
    assert len(markup.inline_keyboard[0]) == 1
    button = markup.inline_keyboard[0][0]
    assert button.text == state_view.REFRESH_LABEL
    assert button.callback_data == "st:r"


# --- (e) router wiring ------------------------------------------------------


def _command_update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
        },
    }


def _callback_update(update_id: int, data: str, *, message_id: int, inaccessible: bool = False) -> dict:
    message: dict = {"message_id": message_id, "chat": {"id": CHAT_ID, "type": "private"}}
    if inaccessible:
        message["date"] = 0
    else:
        message["date"] = 0
        message["text"] = "…"
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": CHAT_ID, "is_bot": False, "first_name": "Test"},
            "chat_instance": "ci",
            "data": data,
            "message": message,
        },
    }


async def _seed(sessionmaker, *update_ids: int) -> None:
    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=CHAT_ID, timezone=TIMEZONE))
        await session.commit()
        for update_id in update_ids:
            session.add(TelegramUpdate(update_id=update_id, payload={}))
        await session.commit()


def _build_dp(sessionmaker) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(
        build_router(sessionmaker, Settings(_env_file=None), FakeLLMProvider(), clock=FrozenClock(NOW))
    )
    return dp


async def _feed(dp: Dispatcher, bot: Bot, payload: dict) -> None:
    await dp.feed_update(bot, Update.model_validate(payload, context={"bot": bot}))


async def test_state_sends_one_rich_message_with_the_refresh_keyboard(sessionmaker):
    await _seed(sessionmaker, 1)
    bot, fake = make_bot()
    dp = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/state"))

    assert len(fake.rich) == 1
    assert fake.sent == []
    markup = fake.rich[0].reply_markup
    assert markup.inline_keyboard[0][0].callback_data == "st:r"


class _RichMessageFailsSession(FakeSession):
    """A FakeSession whose sendRichMessage always rejects, like a
    Telegram client old enough to not understand Bot API 10.1 would."""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        if isinstance(method, SendRichMessage):
            raise TelegramBadRequest(method=method, message="Bad Request: RICH_MESSAGE_INVALID")
        return await super().make_request(bot, method, timeout)


async def test_state_falls_back_to_plain_text_when_the_rich_message_is_rejected(sessionmaker):
    await _seed(sessionmaker, 1)
    fake = _RichMessageFailsSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/state"))

    assert fake.rich == []
    assert len(fake.sent) == 1
    assert "Персона:" in fake.sent[0].text


async def test_state_on_the_web_sink_still_sends_plain_text(sessionmaker):
    await _seed(sessionmaker, 1)
    bot, fake = make_bot()
    bot.is_web_sink = True
    dp = _build_dp(sessionmaker)

    await _feed(dp, bot, _command_update(1, "/state"))

    assert fake.rich == []
    assert len(fake.sent) == 1
    assert "Персона:" in fake.sent[0].text


async def test_menu_state_action_also_goes_through_the_rich_path(sessionmaker):
    """`/menu`'s "state" button calls the same `state()` handler."""
    await _seed(sessionmaker, 1, 2)
    bot, fake = make_bot()
    dp = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/menu"))

    await _feed(dp, bot, _callback_update(2, "mn:a:state", message_id=1))

    assert len(fake.rich) == 1


async def test_refresh_callback_edits_in_place_and_answers_obnovleno(sessionmaker):
    await _seed(sessionmaker, 1, 2)
    bot, fake = make_bot()
    dp = _build_dp(sessionmaker)
    await _feed(dp, bot, _command_update(1, "/state"))

    await _feed(dp, bot, _callback_update(2, "st:r", message_id=1))

    assert len(fake.edits) == 1
    edit = fake.edits[0]
    assert edit.rich_message is not None
    assert edit.reply_markup.inline_keyboard[0][0].callback_data == "st:r"
    assert fake.answered[-1].text == "Обновлено"


async def test_refresh_callback_on_an_inaccessible_message_answers_stale(sessionmaker):
    await _seed(sessionmaker, 1)
    bot, fake = make_bot()
    dp = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, "st:r", message_id=999, inaccessible=True))

    assert fake.edits == []
    assert fake.answered[-1].text == "Устарело."


async def test_refresh_callback_falls_back_to_a_plain_message_on_a_real_rejection(sessionmaker):
    class _EditRichFailsSession(FakeSession):
        async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
            if isinstance(method, EditMessageText) and method.rich_message is not None:
                raise TelegramBadRequest(method=method, message="Bad Request: RICH_MESSAGE_INVALID")
            return await super().make_request(bot, method, timeout)

    await _seed(sessionmaker, 1)
    fake = _EditRichFailsSession()
    bot = Bot(token="123456:TESTTOKEN", session=fake)
    dp = _build_dp(sessionmaker)

    await _feed(dp, bot, _callback_update(1, "st:r", message_id=999))

    assert fake.edits == []
    assert len(fake.sent) == 1
    assert "Персона:" in fake.sent[0].text
    assert fake.answered[-1].text == "Обновлено"
