"""Runtime model choice in the menu: ⚙️ Настройки › 🧠 Модели and its role
cards (app/tg/menu.py), dispatched by app/tg/router.py to app/llm/
models.py's resolver after app/llm/catalog.py's checks, and /state's
model lines (docs/decisions.md, "Runtime model choice").

The pure half renders sections; the router half drives `build_router`
with real updates against the throwaway database, with a real
`ModelCatalog` over tests/test_model_catalog.py's `FakeClient` -- nothing
reaches OpenRouter. Each test names the rule it pins:

- every role has a card, Telegram only, whose buttons carry an index or
  `off`, never a model id, within 64 bytes;
- a pick (or a checked custom id) leads to a confirm card with the
  plan's wording, and only its «Да» switches -- once, however often it
  is pressed;
- ↩️ «По умолчанию» is drawn only while a role is overridden, and asks
  first too;
- refusals answer in one short line and send no card;
- a forged press from the web does nothing;
- /state shows one line per role, marked when it differs from env.
"""

from __future__ import annotations

import decimal

import pytest
from aiogram import Dispatcher
from conftest import FakeLLMProvider, flatten_rich_message, make_bot, rich_callback_data
from sqlalchemy import select
from test_menu import _callback_update, _command_update, _feed, _seed
from test_menu_flows import NOW
from test_model_catalog import FakeClient, _endpoint, _listing
from test_model_resolver import _provider, _say

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import LlmModelOverride, SpendLedger, StateChange
from app.llm import models as llm_models
from app.llm.catalog import REFUSALS, ModelCatalog
from app.llm.models import ModelPrices, ModelResolver
from app.tg import menu, state_view
from app.tg.router import _format_state, build_router

MENU_MESSAGE = 1

PROSE_TWO = "vendor/prose-two"
GOOD = "vendor/good-model"
DECIDER = "vendor/decider"

ENV = {
    "LLM_MODEL": "vendor/persona-env",
    "LLM_MODEL_CHEAP": "vendor/cheap-env",
    "LLM_MODEL_SAFETY": "vendor/safety-env",
    "LLM_MODEL_JUDGE": "vendor/judge-env",
    "MODEL_CHOICES": (
        "vendor/persona-env|Persona|prose;"
        f"{PROSE_TWO}|Prose Two|prose;"
        "vendor/judge-env|Judge|json;"
        "vendor/jsonish|JSONish|json;"
        "typesafe/jev-1.14|Jev|decisions"
    ),
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, TZ_DEFAULT="Europe/Paris", **{**ENV, **overrides})


SETTINGS = _settings()


def _client() -> FakeClient:
    prose = _endpoint(PROSE_TWO, "zdrhost", params=("temperature",))
    good = _endpoint(GOOD, "zdrhost", prompt="0.000001", completion="0.000002")
    probe = {
        "answers": {"probe": {"choice": "ok", "confidence": 0.9,
                               "probabilities": {"ok": 0.9, "no": 0.1}}},
        "usage": {"input_tokens": 12, "output_tokens": 0, "cost": 0.00001},
    }
    return FakeClient(
        {
            f"/models/{PROSE_TWO}/endpoints": _listing(PROSE_TWO, prose),
            f"/models/{GOOD}/endpoints": _listing(GOOD, good),
            f"/models/{DECIDER}/endpoints": _listing(DECIDER, _endpoint(DECIDER, "zdrhost")),
            "/endpoints/zdr": {"data": [prose, good, _endpoint(DECIDER, "zdrhost")]},
        },
        decisions={DECIDER: probe},
    )


async def _build(sessionmaker, *, web=False, catalog=True, resolver=None, client=None):
    bot, fake = make_bot()
    if web:
        bot.is_web_sink = True
    if resolver is None:
        resolver = await ModelResolver.create(SETTINGS, sessionmaker)
    client = client or _client()
    dp = Dispatcher()
    dp.include_router(
        build_router(
            sessionmaker,
            SETTINGS,
            FakeLLMProvider(),
            clock=FrozenClock(NOW),
            model_catalog=ModelCatalog(client, SETTINGS) if catalog else None,
            model_resolver=resolver,
        )
    )
    return dp, bot, fake, resolver, client


async def _tap(dp, bot, update_id: int, data: str, *, message_id: int = MENU_MESSAGE) -> None:
    await _feed(dp, bot, _callback_update(update_id, data, message_id=message_id))


async def _say_line(dp, bot, update_id: int, line: str) -> None:
    await _feed(dp, bot, _command_update(update_id, line))


def _last_id(fake) -> int:
    return fake._next_message_id - 1


async def _changes(sessionmaker, role: str) -> list[StateChange]:
    async with sessionmaker() as session:
        rows = await session.execute(
            select(StateChange).where(StateChange.field == f"model:{role}").order_by(StateChange.id)
        )
        return list(rows.scalars())


def _yes(markup) -> str:
    return markup.inline_keyboard[0][0].callback_data


# --- pure: the sections ---------------------------------------------------------


def test_the_models_card_lists_every_role_and_marks_overrides():
    view = menu.MenuView(
        models=(
            menu.ModelState("persona", PROSE_TWO, True),
            menu.ModelState("garden", "vendor/safety-env", False, True),
        )
    )
    text, markup = menu.render("models", SETTINGS, web=False, view=view)
    assert "Чат: prose-two ✎" in text
    assert "Сводки: cheap-env" in text and "Судья: judge-env" in text
    assert "Сад: = проверки" in text and "Welfare-тень: выкл" in text
    assert menu.MODELS_LEGEND in text
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert datas == [f"mn:s:model_{role}" for role in llm_models.ROLES] + ["mn:s:settings", "mn:x"]
    # Nothing overridden: no legend.
    plain, _ = menu.render("models", SETTINGS, web=False)
    assert menu.MODELS_LEGEND not in plain


def test_model_sections_are_telegram_only_and_reached_from_settings():
    for section in ("models", *menu.MODEL_SECTIONS):
        assert menu.render(section, SETTINGS, web=True) is None
        assert menu.render_rich(section, SETTINGS, web=True) is None
    assert ["mn:s:models"] in [
        [b.callback_data for b in row] for row in menu.render("settings", SETTINGS, web=False)[1].inline_keyboard
    ]
    assert "mn:s:models" not in str(menu.render("settings", SETTINGS, web=True)[1])
    assert menu.MODEL_KEYS <= menu.TELEGRAM_ONLY


def test_a_role_card_offers_the_fitting_picks_with_prices_and_no_model_id():
    prices = ModelPrices(decimal.Decimal("0.3"), None, decimal.Decimal("0.5"))
    view = menu.MenuView(model_prices=((PROSE_TWO, prices), ("vendor/persona-env", prices)))
    text, markup = menu.render("model_persona", SETTINGS, web=False, view=view)
    assert "Сейчас: vendor/persona-env (по умолчанию) · $0.30 / $0.50 за 1M" in text
    rows = [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]
    assert rows == [
        [("✓ Persona · $0.30 / $0.50", "mn:a:mdl_persona:0")],
        [("Prose Two · $0.30 / $0.50", "mn:a:mdl_persona:1")],
        [("✏️ Своя модель (id)", "mn:i:mdl_custom_persona")],
        [("‹ Модели", "mn:s:models"), ("✕ Закрыть", "mn:x")],
    ]
    for section in menu.MODEL_SECTIONS:
        for view in (menu.MenuView(), menu.MenuView(models=tuple(
            menu.ModelState(role, "x" * 40 + "/y", True) for role in llm_models.ROLES
        ))):
            _, markup = menu.render(section, SETTINGS, web=False, view=view)
            for row in markup.inline_keyboard:
                for button in row:
                    assert menu.fits_callback(button.callback_data)
                    assert "/" not in button.callback_data, button.callback_data


def test_every_fixed_model_callback_fits_and_the_documented_longest_is_the_longest():
    """Every model key in each form it is drawn as -- a pick with every
    index and `off`, the ✏️ prompt, the ↩️ action and every «Да» --
    stays within 64 bytes; and the module docstring's budget names the
    real longest fixed one with its real length, so the headroom it
    states is the headroom there is."""
    fixed = [
        *(f"{menu.ACTION_PREFIX}{key}" for key in menu.MODEL_RESETS),
        *(f"{menu.INPUT_PREFIX}{key}" for key in menu.MODEL_CUSTOM),
        *(menu.yes_data(key) for key in (*menu.MODEL_PICKS, *menu.MODEL_RESETS)),
    ]
    for data in fixed:
        assert menu.fits_callback(data), data
    for key, role in menu.MODEL_PICKS.items():
        for arg in (*map(str, range(len(menu.choices_for(role, SETTINGS)))), "off"):
            assert menu.fits_callback(f"{menu.ACTION_PREFIX}{key}:{arg}"), (key, arg)
    longest = max(fixed, key=lambda data: len(data.encode("utf-8")))
    assert f'"{longest}", is {len(longest.encode("utf-8"))} bytes' in " ".join(
        menu.__doc__.split()
    )


def test_reset_is_drawn_only_while_overridden_and_off_only_while_the_shadow_runs():
    def keys(section, view):
        _, markup = menu.render(section, SETTINGS, web=False, view=view)
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    assert "mn:a:mdl_reset_cheap" not in keys("model_cheap", menu.MenuView())
    overridden = menu.MenuView(models=(menu.ModelState("cheap", PROSE_TWO, True),))
    assert "mn:a:mdl_reset_cheap" in keys("model_cheap", overridden)

    off = keys("model_welfare_shadow", menu.MenuView())
    assert "mn:a:mdl_welfare_shadow:0" in off and "mn:a:mdl_welfare_shadow:off" not in off
    on = menu.MenuView(models=(menu.ModelState("welfare_shadow", "typesafe/jev-1.14", True),))
    assert "mn:a:mdl_welfare_shadow:off" in keys("model_welfare_shadow", on)
    assert menu.model_pick_target("welfare_shadow", "off", SETTINGS) == ""
    assert menu.model_pick_target("persona", "off", SETTINGS) is None
    for bad in (None, "", "-1", "9", " 1", "１", "1000"):
        assert menu.model_pick_target("persona", bad, SETTINGS) is None, bad
    assert menu.model_pick_target("persona", "1", SETTINGS) == PROSE_TWO


def test_confirm_texts_follow_the_plan():
    prices = ModelPrices(decimal.Decimal("0.3"), None, decimal.Decimal("0.075"))
    assert menu.model_confirm_text("persona", PROSE_TWO, prices) == (
        f"Сменить модель «Чат» на {PROSE_TWO}? Вход $0.30, выход $0.075 за 1M. "
        "Влияет на тон и цену."
    )
    safety = menu.model_confirm_text("safety", GOOD, None)
    assert "Цена пока неизвестна." in safety and safety.endswith(menu.MODEL_SAFETY_WARNING)
    assert menu.model_confirm_text("welfare_shadow", "", None) == menu.MODEL_OFF_CONFIRM
    assert _yes(menu.model_confirm_markup("persona", PROSE_TWO)) == "mn:y:mdl_persona"


# --- the router: pick -> card -> «Да» --------------------------------------------


async def test_a_pick_asks_first_then_yes_switches_and_the_next_call_uses_it(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)

    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:s:model_persona")
    shown = flatten_rich_message(fake.edits[-1].rich_message)
    assert "Prose Two · $0.30 / $0.50" in shown
    assert "mn:a:mdl_persona:1" in rich_callback_data(fake.edits[-1].rich_message)

    await _tap(dp, bot, 3, "mn:a:mdl_persona:1")
    card = fake.sent[-1]
    assert card.text == (
        f"Сменить модель «Чат» на {PROSE_TWO}? Вход $0.30, выход $0.50 за 1M. Влияет на тон и цену."
    )
    assert _yes(card.reply_markup) == "mn:y:mdl_persona"
    card_id = _last_id(fake)
    assert resolver.effective("persona") == "vendor/persona-env"
    assert await _changes(sessionmaker, "persona") == []

    await _tap(dp, bot, 4, "mn:y:mdl_persona", message_id=card_id)
    assert resolver.effective("persona") == PROSE_TWO
    (change,) = await _changes(sessionmaker, "persona")
    assert (change.old_value, change.new_value, change.source) == (
        "vendor/persona-env", PROSE_TWO, "button"
    )
    async with sessionmaker() as session:
        row = await session.get(LlmModelOverride, "persona")
    assert row.model == PROSE_TWO and row.price_in == decimal.Decimal("0.3")
    card_edits = [e for e in fake.edits if e.message_id == card_id]
    assert card_edits[-1].text == f"Готово: «Чат» теперь на {PROSE_TWO}."
    # The role card under the pick is re-rendered in place, ticked and resettable.
    rerendered = flatten_rich_message(fake.edits[-1].rich_message)
    assert fake.edits[-1].message_id == MENU_MESSAGE
    assert "✓ Prose Two" in rerendered and "↩️ По умолчанию" in rerendered

    provider, client = _provider(resolver, role="persona")
    await _say(provider)
    assert client.chat.completions.models == [PROSE_TWO]


async def test_a_double_tapped_yes_switches_once(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:mdl_cheap:1")
    card_id = _last_id(fake)

    await _tap(dp, bot, 3, "mn:y:mdl_cheap", message_id=card_id)
    await _tap(dp, bot, 4, "mn:y:mdl_cheap", message_id=card_id)

    assert resolver.effective("cheap") == PROSE_TWO
    assert len(await _changes(sessionmaker, "cheap")) == 1
    assert fake.answered[-1].text == "Устарело."


async def test_no_switches_nothing_and_a_later_yes_is_stale(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:mdl_persona:1")
    card_id = _last_id(fake)

    await _tap(dp, bot, 3, "mn:c", message_id=card_id)
    await _tap(dp, bot, 4, "mn:y:mdl_persona", message_id=card_id)

    assert resolver.effective("persona") == "vendor/persona-env"
    assert await _changes(sessionmaker, "persona") == []


async def test_a_yes_whose_card_was_forgotten_by_a_restart_changes_nothing(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:mdl_persona:1")
    card_id = _last_id(fake)

    # A new router is a new process: the pending card is gone.
    dp2, _, _, _, _ = await _build(sessionmaker, resolver=resolver)
    await _tap(dp2, bot, 3, "mn:y:mdl_persona", message_id=card_id)

    assert resolver.effective("persona") == "vendor/persona-env"
    assert [e for e in fake.edits if e.message_id == card_id][-1].text == menu.MODEL_CARD_GONE


async def test_picking_the_current_model_says_so_and_sends_no_card(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, _, _ = await _build(sessionmaker)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:mdl_persona:0")
    assert fake.sent[-1].text == "«Чат» уже на этой модели."
    assert fake.sent[-1].reply_markup is None


@pytest.mark.parametrize(
    "data",
    ["mn:a:mdl_persona", "mn:a:mdl_persona:9", "mn:a:mdl_persona:-1", "mn:a:mdl_persona:off",
     "mn:a:mdl_reset_persona:x", "mn:i:mdl_custom_persona:x", "mn:y:mdl_persona:1"],
)
async def test_malformed_model_presses_are_stale(sessionmaker, data):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await _say_line(dp, bot, 1, "/menu")
    sent = len(fake.sent)
    await _tap(dp, bot, 2, data)
    assert fake.answered[-1].text == "Устарело."
    assert len(fake.sent) == sent
    assert all(not resolver.overridden(role) for role in llm_models.ROLES)


async def test_judge_onto_the_persona_model_is_refused_without_a_card(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await resolver.set("persona", "vendor/jsonish")
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:mdl_judge:1")  # JSONish
    assert fake.sent[-1].text == REFUSALS["judge_equals_persona"]
    assert resolver.effective("judge") == "vendor/judge-env"


async def test_without_a_resolver_a_switch_says_it_is_unavailable(sessionmaker, monkeypatch):
    await _seed(sessionmaker, *range(1, 10))
    monkeypatch.setattr(llm_models, "_installed", None)
    bot, fake = make_bot()
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, SETTINGS, FakeLLMProvider(), clock=FrozenClock(NOW)))
    await _feed(dp, bot, _command_update(1, "/menu"))
    await _feed(dp, bot, _callback_update(2, "mn:s:model_persona", message_id=MENU_MESSAGE))
    assert "vendor/persona-env (по умолчанию)" in flatten_rich_message(fake.edits[-1].rich_message)
    await _feed(dp, bot, _callback_update(3, "mn:a:mdl_persona:1", message_id=MENU_MESSAGE))
    assert fake.sent[-1].text == menu.MODELS_UNAVAILABLE


# --- the router: ✏️ «Своя модель» -------------------------------------------------


async def test_a_custom_id_is_checked_then_confirmed(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, client = await _build(sessionmaker)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:i:mdl_custom_safety")
    assert fake.sent[-1].text == menu.prompt_text("mdl_custom_safety")

    await _say_line(dp, bot, 3, f"  {GOOD} ")
    card = fake.sent[-1]
    assert card.text.startswith(f"Сменить модель «Проверки» на {GOOD}? Вход $1.00, выход $2.00 за 1M.")
    assert card.text.endswith(menu.MODEL_SAFETY_WARNING)
    assert f"/models/{GOOD}/endpoints" in client.gets
    card_id = _last_id(fake)

    await _tap(dp, bot, 4, "mn:y:mdl_safety", message_id=card_id)
    assert resolver.effective("safety") == GOOD
    # The followers move with it.
    assert resolver.effective("garden") == GOOD and resolver.follows_safety("garden")
    # Typed in a prompt: there is no role card under it, so one is sent.
    assert fake.rich and "Модель проверок" in flatten_rich_message(fake.rich[-1].rich_message)


@pytest.mark.parametrize(
    ("line", "code"),
    [("vendor/does-not-exist", "missing"), ("not an id", "missing"), ("x" * 200, "missing")],
)
async def test_a_custom_id_that_fails_a_check_is_answered_without_a_card(sessionmaker, line, code):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:i:mdl_custom_cheap")
    await _say_line(dp, bot, 3, line)
    assert fake.sent[-1].text == REFUSALS[code]
    assert fake.sent[-1].reply_markup is None
    assert not resolver.overridden("cheap")


async def test_a_custom_id_without_a_catalog_is_unreachable_but_a_curated_pick_works(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker, catalog=False)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:i:mdl_custom_cheap")
    await _say_line(dp, bot, 3, GOOD)
    assert fake.sent[-1].text == REFUSALS["unreachable"]

    await _tap(dp, bot, 4, "mn:a:mdl_cheap:1")
    assert "Цена пока неизвестна." in fake.sent[-1].text
    await _tap(dp, bot, 5, "mn:y:mdl_cheap", message_id=_last_id(fake))
    assert resolver.effective("cheap") == PROSE_TWO
    assert resolver.override("cheap").prices == llm_models.NO_PRICES


async def test_a_custom_shadow_id_is_probed_and_the_probe_is_ledgered(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, _, client = await _build(sessionmaker)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:i:mdl_custom_welfare_shadow")
    await _say_line(dp, bot, 3, DECIDER)
    assert len(client.posts) == 1
    assert fake.sent[-1].text.startswith(f"Сменить модель «Welfare-тень» на {DECIDER}?")
    async with sessionmaker() as session:
        rows = list((await session.execute(select(SpendLedger))).scalars())
    assert [(r.category, r.model, r.tokens_in) for r in rows] == [("welfare_shadow", DECIDER, 12)]


# --- the router: ↩️ and «выкл» ----------------------------------------------------


async def test_reset_asks_first_and_returns_the_role_to_env(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await resolver.set("persona", PROSE_TWO)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:mdl_reset_persona")
    card = fake.sent[-1]
    assert card.text == "Вернуть «Чат» на модель по умолчанию — vendor/persona-env?"
    assert _yes(card.reply_markup) == "mn:y:mdl_reset_persona"
    assert resolver.effective("persona") == PROSE_TWO

    await _tap(dp, bot, 3, "mn:y:mdl_reset_persona", message_id=_last_id(fake))
    assert resolver.effective("persona") == "vendor/persona-env"
    assert not resolver.overridden("persona")
    assert len(await _changes(sessionmaker, "persona")) == 2
    assert "↩️ По умолчанию" not in flatten_rich_message(fake.edits[-1].rich_message)


async def test_a_reset_card_whose_default_moved_since_is_stale_and_changes_nothing(sessionmaker):
    """«Сад» goes back to whatever «Проверки» is on. A safety switch
    between the ↩️ card and its «Да» changes that default, so the card
    no longer names the model «Да» would apply: it is retired as stale
    and the garden keeps its own model -- every switch is confirmed for
    the model it applies."""
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await resolver.set("garden", "vendor/jsonish")
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:mdl_reset_garden")
    card_id = _last_id(fake)
    assert fake.sent[-1].text == "Вернуть «Сад» на модель по умолчанию — vendor/safety-env?"

    await resolver.set("safety", "vendor/judge-env")
    await _tap(dp, bot, 3, "mn:y:mdl_reset_garden", message_id=card_id)

    assert [e for e in fake.edits if e.message_id == card_id][-1].text == menu.MODEL_CARD_GONE
    assert resolver.overridden("garden")
    assert resolver.effective("garden") == "vendor/jsonish"
    assert len(await _changes(sessionmaker, "garden")) == 1


async def test_reset_of_a_role_on_its_default_sends_no_card(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, _, _ = await _build(sessionmaker)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:mdl_reset_persona")
    assert fake.sent[-1].text == "«Чат» и так на модели по умолчанию."


async def test_the_shadow_turns_on_from_the_list_and_off_again(sessionmaker):
    await _seed(sessionmaker, *range(1, 12))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await _say_line(dp, bot, 1, "/menu")
    await _tap(dp, bot, 2, "mn:a:mdl_welfare_shadow:0")
    await _tap(dp, bot, 3, "mn:y:mdl_welfare_shadow", message_id=_last_id(fake))
    assert resolver.effective("welfare_shadow") == "typesafe/jev-1.14"

    await _tap(dp, bot, 4, "mn:a:mdl_welfare_shadow:off")
    card = fake.sent[-1]
    assert card.text == menu.MODEL_OFF_CONFIRM
    assert card.reply_markup.inline_keyboard[0][0].text == menu.MODEL_OFF_YES
    await _tap(dp, bot, 5, "mn:y:mdl_welfare_shadow", message_id=_last_id(fake))
    assert resolver.effective("welfare_shadow") == ""
    assert resolver.overridden("welfare_shadow")
    assert [e for e in fake.edits if e.message_id == _last_id(fake)][-1].text == menu.MODEL_OFF_APPLIED


# --- the web: never drawn, refused when forged ----------------------------------


@pytest.mark.parametrize(
    "data",
    ["mn:s:models", "mn:s:model_persona", "mn:a:mdl_persona:1", "mn:y:mdl_persona",
     "mn:i:mdl_custom_persona", "mn:a:mdl_reset_persona", "mn:y:mdl_reset_persona"],
)
async def test_a_forged_model_press_from_the_web_does_nothing(sessionmaker, data):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, client = await _build(sessionmaker, web=True)
    await resolver.set("persona", PROSE_TWO)
    await _tap(dp, bot, 1, data)
    assert fake.answered[-1].text == "Устарело."
    assert fake.sent == [] and fake.edits == []
    assert resolver.effective("persona") == PROSE_TWO
    assert client.gets == []


# --- /state ------------------------------------------------------------------------


def test_state_view_model_rows_mark_what_differs_from_env():
    rows = state_view.model_rows(SETTINGS, (("Чат", PROSE_TWO, True), ("Welfare-тень", "", False)))
    assert rows == [("Модель «Чат»", f"{PROSE_TWO} · изменена"), ("Модель «Welfare-тень»", "выкл")]
    assert state_view.model_rows(SETTINGS, None) == [("Модель", "vendor/persona-env")]


async def test_state_shows_one_line_per_role_marked_when_overridden(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await resolver.set("safety", GOOD)
    await _say_line(dp, bot, 1, "/state")
    text = flatten_rich_message(fake.rich[-1].rich_message)
    assert "vendor/persona-env" in text
    assert f"{GOOD} · изменена" in text
    for label in menu.MODEL_ROLE_LABELS.values():
        assert f"Модель «{label}»" in text
    # Garden follows the safety switch, so it differs from env too.
    assert text.count("· изменена") == 3


async def test_plain_state_lines_match_the_rich_ones(sessionmaker):
    await _seed(sessionmaker, *range(1, 10))
    async with sessionmaker() as session:
        from app.core.state import get_state

        user_state = await get_state(session)
    models = (("Чат", PROSE_TWO, True), ("Сводки", "vendor/cheap-env", False))
    text = _format_state(user_state, decimal.Decimal(0), SETTINGS, FrozenClock(NOW), models=models)
    assert text.endswith(
        f"Модель «Чат»: {PROSE_TWO} · изменена\nМодель «Сводки»: vendor/cheap-env"
    )
    legacy = _format_state(user_state, decimal.Decimal(0), SETTINGS, FrozenClock(NOW))
    assert legacy.endswith("Модель: vendor/persona-env")


async def test_state_is_one_line_per_role_on_the_web_as_well(sessionmaker):
    """The web sink gets /state as plain text, never a rich message; the
    lines are the same ones, marked the same way."""
    await _seed(sessionmaker, *range(1, 10))
    dp, bot, fake, resolver, _ = await _build(sessionmaker, web=True)
    await resolver.set("cheap", PROSE_TWO)
    await _say_line(dp, bot, 1, "/state")
    text = fake.sent[-1].text
    assert fake.rich == []
    assert f"Модель «Сводки»: {PROSE_TWO} · изменена" in text
    assert "Модель «Чат»: vendor/persona-env\n" in text
    assert text.count("· изменена") == 1
    for label in menu.MODEL_ROLE_LABELS.values():
        assert f"Модель «{label}»" in text


async def test_state_without_a_resolver_reads_env_and_marks_nothing(sessionmaker, monkeypatch):
    await _seed(sessionmaker, *range(1, 10))
    monkeypatch.setattr(llm_models, "_installed", None)
    bot, fake = make_bot()
    dp = Dispatcher()
    dp.include_router(build_router(sessionmaker, SETTINGS, FakeLLMProvider(), clock=FrozenClock(NOW)))
    await _feed(dp, bot, _command_update(1, "/state"))
    text = flatten_rich_message(fake.rich[-1].rich_message)
    assert "vendor/persona-env" in text and "vendor/safety-env" in text
    assert "изменена" not in text
    assert "Модель «Welfare-тень»" in text


async def test_state_marks_follow_the_difference_from_env_not_the_row(sessionmaker):
    """A row that pins a role to the model env gives it is no change; a
    follower that moves with the safety switch is one, though it has no
    row; and a reset takes every mark away."""
    await _seed(sessionmaker, *range(1, 12))
    dp, bot, fake, resolver, _ = await _build(sessionmaker)
    await resolver.set("garden", "vendor/safety-env")  # pinned, but where env already is
    await _say_line(dp, bot, 1, "/state")
    assert "изменена" not in flatten_rich_message(fake.rich[-1].rich_message)

    await resolver.set("safety", GOOD)
    await _say_line(dp, bot, 2, "/state")
    text = flatten_rich_message(fake.rich[-1].rich_message)
    # safety and suggest follow it; garden is pinned and stays on env.
    assert text.count("· изменена") == 2

    await resolver.reset("safety")
    await _say_line(dp, bot, 3, "/state")
    assert "изменена" not in flatten_rich_message(fake.rich[-1].rich_message)
