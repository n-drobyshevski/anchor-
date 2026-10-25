"""Runtime model choice: app/llm/models.py's `ModelResolver`, the
providers that resolve through it (app/llm/openrouter.py), and the
override prices app/core/spend.py bills at (docs/decisions.md, "Runtime
model choice").

Every provider here talks to a capturing fake client, never the
network; the resolver tests use the throwaway test database.
"""

from __future__ import annotations

import asyncio
import decimal

import pytest
from openai.types.chat import ChatCompletion
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.core import spend
from app.db.models import LlmModelOverride, StateChange
from app.llm import models as llm_models
from app.llm.models import ROLES, ModelPrices, ModelResolver
from app.llm.openrouter import OpenRouterDecisionProvider, OpenRouterProvider
from app.llm.provider import Choice, LLMError, LLMMessage, LLMUsage

ENV = {
    "LLM_MODEL": "vendor/persona-env",
    "LLM_MODEL_CHEAP": "vendor/cheap-env",
    "LLM_MODEL_SAFETY": "vendor/safety-env",
    "LLM_MODEL_JUDGE": "vendor/judge-env",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**ENV, **overrides})


@pytest.fixture()
def uninstall():
    """Leave no resolver registered for spend after the test."""
    yield
    llm_models.install(None)


# --- the role list is the table's CHECK -----------------------------------------


def test_roles_equal_the_table_check():
    """ck_llm_model_override_role and ROLES must name the same roles: a
    role missing from the CHECK could never be saved, and one missing
    from ROLES would be a row nothing reads."""
    constraint = next(
        c for c in LlmModelOverride.__table__.constraints if c.name == "ck_llm_model_override_role"
    )
    named = {part.strip(" '()") for part in str(constraint.sqltext).split("in", 1)[1].split(",")}
    assert named == set(ROLES)
    assert set(llm_models.ROLE_SETTINGS) == set(ROLES)


# --- fallback chains (no database needed until a write) ------------------------


def test_every_role_resolves_to_env_before_any_override():
    resolver = ModelResolver(_settings(), sessionmaker=None)
    assert resolver.effective("persona") == "vendor/persona-env"
    assert resolver.effective("cheap") == "vendor/cheap-env"
    assert resolver.effective("safety") == "vendor/safety-env"
    assert resolver.effective("judge") == "vendor/judge-env"
    # "" in env means the safety model for these two, and off for the shadow.
    assert resolver.effective("garden") == "vendor/safety-env"
    assert resolver.effective("suggest") == "vendor/safety-env"
    assert resolver.effective("welfare_shadow") == ""
    assert not any(resolver.overridden(role) for role in ROLES)
    assert resolver.follows_safety("garden") and resolver.follows_safety("suggest")


def test_garden_and_suggest_env_models_beat_the_env_safety_model():
    resolver = ModelResolver(
        _settings(GARDEN_MODEL="vendor/garden-env", INTEREST_SUGGEST_MODEL="vendor/suggest-env"),
        sessionmaker=None,
    )
    assert resolver.effective("garden") == "vendor/garden-env"
    assert resolver.effective("suggest") == "vendor/suggest-env"
    assert not resolver.follows_safety("garden")


def test_unknown_role_is_refused():
    resolver = ModelResolver(_settings(), sessionmaker=None)
    with pytest.raises(ValueError):
        resolver.effective("narrator")


async def test_set_and_reset_follow_the_chains(sessionmaker):
    settings = _settings(GARDEN_MODEL="vendor/garden-env")
    resolver = await ModelResolver.create(settings, sessionmaker)

    await resolver.set("safety", "vendor/safety-picked")
    assert resolver.effective("safety") == "vendor/safety-picked"
    # garden → override(safety) → GARDEN_MODEL → LLM_MODEL_SAFETY: the
    # safety switch moves the garden even past its own env model ...
    assert resolver.effective("garden") == "vendor/safety-picked"
    assert resolver.follows_safety("garden")
    assert resolver.effective("suggest") == "vendor/safety-picked"
    # ... while env alone still says what it said.
    assert resolver.env_default("garden") == "vendor/garden-env"

    await resolver.set("garden", "vendor/garden-picked")
    assert resolver.effective("garden") == "vendor/garden-picked"
    assert resolver.default("garden") == "vendor/safety-picked"
    assert not resolver.follows_safety("garden")

    assert await resolver.reset("safety") is True
    assert resolver.effective("safety") == "vendor/safety-env"
    assert resolver.effective("garden") == "vendor/garden-picked"
    assert resolver.effective("suggest") == "vendor/safety-env"

    assert await resolver.reset("garden") is True
    assert resolver.effective("garden") == "vendor/garden-env"
    assert await resolver.reset("garden") is False


async def test_set_writes_the_row_and_its_audit_and_load_reads_it_back(sessionmaker):
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    prices = ModelPrices(decimal.Decimal("0.30"), None, decimal.Decimal("0.50"))
    assert await resolver.set("persona", "vendor/persona-picked", prices) == "vendor/persona-picked"

    async with sessionmaker() as session:
        row = (await session.execute(select(LlmModelOverride))).scalar_one()
        audits = (await session.execute(select(StateChange))).scalars().all()
    assert (row.role, row.model) == ("persona", "vendor/persona-picked")
    assert (row.price_in, row.price_cached, row.price_out) == (
        decimal.Decimal("0.30"), None, decimal.Decimal("0.50")
    )
    assert [(a.field, a.old_value, a.new_value, a.source) for a in audits] == [
        ("model:persona", "vendor/persona-env", "vendor/persona-picked", "button")
    ]

    # A second process start sees the same thing.
    fresh = await ModelResolver.create(_settings(), sessionmaker)
    assert fresh.effective("persona") == "vendor/persona-picked"
    assert fresh.override("persona").prices == prices

    # Upsert: a second pick replaces the row, never adds one.
    await resolver.set("persona", "vendor/persona-again")
    async with sessionmaker() as session:
        rows = (await session.execute(select(LlmModelOverride))).scalars().all()
    assert [(r.model, r.price_in) for r in rows] == [("vendor/persona-again", None)]

    await resolver.reset("persona")
    async with sessionmaker() as session:
        assert (await session.execute(select(LlmModelOverride))).first() is None
        last = (
            await session.execute(select(StateChange).order_by(StateChange.id.desc()))
        ).scalars().first()
    assert (last.field, last.old_value, last.new_value) == (
        "model:persona", "vendor/persona-again", "vendor/persona-env"
    )


async def test_welfare_shadow_may_be_switched_off_and_no_other_role(sessionmaker):
    resolver = await ModelResolver.create(
        _settings(WELFARE_SHADOW_MODEL="typesafe/jev-1.14"), sessionmaker
    )
    assert resolver.effective("welfare_shadow") == "typesafe/jev-1.14"
    await resolver.set("welfare_shadow", "")
    assert resolver.effective("welfare_shadow") == ""
    assert resolver.overridden("welfare_shadow")
    with pytest.raises(ValueError):
        await resolver.set("persona", "  ")
    with pytest.raises(ValueError):
        await resolver.set("persona", "vendor/x", ModelPrices(decimal.Decimal(-1), None, None))
    assert not resolver.overridden("persona")


async def test_clear_forgets_the_cache_after_delete(sessionmaker):
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    await resolver.set("cheap", "vendor/cheap-picked")
    resolver.clear()
    assert resolver.effective("cheap") == "vendor/cheap-env"


async def test_a_failed_write_leaves_the_cache_alone(sessionmaker):
    """The cache changes only after the commit: a row the CHECK refuses
    (here a role the resolver is tricked into accepting) must not leave
    the dict claiming it."""
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    original = llm_models.ROLES
    llm_models.ROLES = (*original, "narrator")
    llm_models.ROLE_SETTINGS["narrator"] = "LLM_MODEL"
    try:
        with pytest.raises(IntegrityError):
            await resolver.set("narrator", "vendor/x")
        assert "narrator" not in resolver._overrides
    finally:
        llm_models.ROLES = original
        llm_models.ROLE_SETTINGS.pop("narrator")


async def test_the_cache_holds_exactly_what_a_restart_reads_back(sessionmaker):
    """Prices are rounded to the column's six places before they reach
    the cache, so the figure a call is billed at today is the one it is
    billed at after a restart -- and one too big for the column is
    refused before it can fail inside the commit."""
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    await resolver.set(
        "persona",
        "vendor/x",
        ModelPrices(decimal.Decimal("0.1234567"), decimal.Decimal("0.0000004"), decimal.Decimal(2)),
    )
    fresh = await ModelResolver.create(_settings(), sessionmaker)
    assert fresh.override("persona").prices == resolver.override("persona").prices
    assert resolver.override("persona").prices.price_in == decimal.Decimal("0.123457")
    with pytest.raises(ValueError):
        await resolver.set("cheap", "vendor/y", ModelPrices(decimal.Decimal(10) ** 6, None, None))
    assert not resolver.overridden("cheap")


async def test_concurrent_picks_leave_the_cache_equal_to_the_table(sessionmaker):
    """Two «Да» presses on different roles' cards, or a pick racing a
    reset, are serialised: every change gets its audit row, each audit
    row's old value is the one before it, and the cache ends where the
    table does."""
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    picks = [f"vendor/persona-{n}" for n in range(6)]
    await asyncio.gather(*(resolver.set("persona", model) for model in picks))

    async with sessionmaker() as session:
        row = await session.get(LlmModelOverride, "persona")
        audits = (
            await session.execute(
                select(StateChange).where(StateChange.field == "model:persona").order_by(StateChange.id)
            )
        ).scalars().all()
    assert resolver.effective("persona") == row.model
    assert sorted(a.new_value for a in audits) == sorted(picks)
    # A chain, not six changes from env: each row starts where the last ended.
    assert audits[0].old_value == "vendor/persona-env"
    assert [a.old_value for a in audits[1:]] == [a.new_value for a in audits[:-1]]
    assert audits[-1].new_value == row.model

    await asyncio.gather(resolver.reset("persona"), resolver.reset("persona"))
    async with sessionmaker() as session:
        assert await session.get(LlmModelOverride, "persona") is None
        resets = (
            await session.execute(
                select(StateChange).where(StateChange.field == "model:persona")
            )
        ).scalars().all()
    # The second reset found nothing to do and wrote nothing.
    assert len(resets) == len(picks) + 1
    assert not resolver.overridden("persona")


async def test_a_failed_audit_rolls_the_row_back_with_it(sessionmaker, monkeypatch):
    """The row and its `model:<role>` audit land together or not at all:
    if the audit cannot be written, the choice is neither in the table
    nor in the cache, for a set and for a reset alike."""
    resolver = await ModelResolver.create(_settings(), sessionmaker)

    async def explode(*args, **kwargs):
        raise RuntimeError("audit down")

    with monkeypatch.context() as patch:
        patch.setattr("app.core.state.record_change", explode)
        with pytest.raises(RuntimeError):
            await resolver.set("cheap", "vendor/cheap-picked")
    assert not resolver.overridden("cheap")
    async with sessionmaker() as session:
        assert (await session.execute(select(LlmModelOverride))).first() is None

    await resolver.set("cheap", "vendor/cheap-picked")
    with monkeypatch.context() as patch:
        patch.setattr("app.core.state.record_change", explode)
        with pytest.raises(RuntimeError):
            await resolver.reset("cheap")
    # Still picked, in the table and in the cache.
    assert resolver.effective("cheap") == "vendor/cheap-picked"
    async with sessionmaker() as session:
        assert (await session.get(LlmModelOverride, "cheap")).model == "vendor/cheap-picked"


async def test_load_replaces_the_cache_with_the_table(sessionmaker):
    """A row removed behind the resolver's back (a restore, a manual
    fix) stops applying on the next load -- load replaces, never merges."""
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    await resolver.set("cheap", "vendor/cheap-picked")
    await resolver.set("judge", "vendor/judge-picked")
    async with sessionmaker() as session:
        await session.execute(delete(LlmModelOverride).where(LlmModelOverride.role == "cheap"))
        await session.commit()
    assert resolver.effective("cheap") == "vendor/cheap-picked"  # cached
    await resolver.load()
    assert resolver.effective("cheap") == "vendor/cheap-env"
    assert resolver.effective("judge") == "vendor/judge-picked"


async def test_the_audit_names_models_and_nothing_else(sessionmaker):
    """`state_change` is content-free: a role, two model ids and the
    source. The prices that went into the row are not in it."""
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    await resolver.set(
        "suggest", "vendor/suggest-picked", ModelPrices(decimal.Decimal("3.5"), None, decimal.Decimal("7.5"))
    )
    async with sessionmaker() as session:
        (audit,) = (await session.execute(select(StateChange))).scalars().all()
    assert (audit.field, audit.old_value, audit.new_value, audit.source) == (
        "model:suggest", "vendor/safety-env", "vendor/suggest-picked", "button"
    )
    assert "3.5" not in f"{audit.old_value}{audit.new_value}"


# --- the chat provider resolves per request ------------------------------------


def _ok_response() -> ChatCompletion:
    return ChatCompletion.construct(
        id="gen-x",
        created=1,
        model="m",
        object="chat.completion",
        choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ок"}}],
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )


class _Completions:
    def __init__(self):
        self.models: list[str] = []
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.models.append(kwargs["model"])
        self.kwargs = kwargs
        return _ok_response()


class _Client:
    def __init__(self):
        self.chat = type("_Chat", (), {"completions": _Completions()})()


def _provider(resolver, role="cheap", **kwargs):
    client = _Client()
    provider = OpenRouterProvider(
        api_key="",
        model="vendor/cheap-env",
        max_tokens=300,
        temperature=0.5,
        data_collection="deny",
        client=client,
        resolver=resolver,
        role=role,
        **kwargs,
    )
    return provider, client


async def _say(provider):
    return await provider.complete([LLMMessage(role="user", content="привет")], conversation_id="x")


async def test_complete_sends_and_reports_the_resolved_model(sessionmaker):
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    provider, client = _provider(resolver)

    first = await _say(provider)
    await resolver.set("cheap", "vendor/cheap-picked")
    second = await _say(provider)
    await resolver.reset("cheap")
    third = await _say(provider)

    assert client.chat.completions.models == [
        "vendor/cheap-env", "vendor/cheap-picked", "vendor/cheap-env"
    ]
    assert [first.model, second.model, third.model] == client.chat.completions.models
    # The constructor's model is still what `_model` holds.
    assert provider._model == "vendor/cheap-env"


async def test_with_max_tokens_keeps_the_resolver_and_role(sessionmaker):
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    provider, client = _provider(resolver, role="safety")
    wider = provider.with_max_tokens(2000)
    await resolver.set("safety", "vendor/safety-picked")

    response = await _say(wider)
    assert client.chat.completions.kwargs["max_tokens"] == 2000
    assert client.chat.completions.kwargs["model"] == "vendor/safety-picked"
    assert response.model == "vendor/safety-picked"
    assert wider.model == "vendor/safety-picked"


async def test_without_a_resolver_nothing_changes():
    client = _Client()
    provider = OpenRouterProvider(
        api_key="", model="vendor/fixed", max_tokens=10, temperature=0.0,
        data_collection="deny", client=client,
    )
    response = await _say(provider)
    assert client.chat.completions.models == ["vendor/fixed"]
    assert response.model == provider.model == provider._model == "vendor/fixed"
    assert provider.with_max_tokens(20).model == "vendor/fixed"


async def test_a_switch_landing_mid_call_cannot_split_the_request_from_its_report(sessionmaker):
    """The model is resolved once per request: a «Да» that lands while a
    call is out changes the next call, and this one is reported -- and
    billed (app/core/spend.py prices by `LLMResponse.model`) -- as the
    model it was actually sent to."""
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    gate = asyncio.Event()
    entered = asyncio.Event()

    class SlowCompletions(_Completions):
        async def create(self, **kwargs):
            self.models.append(kwargs["model"])
            entered.set()
            await gate.wait()
            return _ok_response()

    provider, client = _provider(resolver)
    client.chat.completions = SlowCompletions()

    call = asyncio.create_task(_say(provider))
    await entered.wait()
    await resolver.set("cheap", "vendor/cheap-picked")
    gate.set()
    response = await call

    assert client.chat.completions.models == ["vendor/cheap-env"]
    assert response.model == "vendor/cheap-env"
    assert provider.model == "vendor/cheap-picked"


async def test_a_switch_changes_the_model_and_nothing_else_in_the_request(sessionmaker):
    """The privacy and shape fields are the provider's, not the role's:
    after a switch a JSON call still demands `require_parameters` and
    `data_collection: deny`, and a searched call still carries the
    pinned Exa plugin -- on the safety role's *new* model."""
    from app.llm.provider import JSONSchema, WebSearch

    resolver = await ModelResolver.create(_settings(), sessionmaker)
    provider, client = _provider(resolver, role="safety", structured_outputs=True)
    await resolver.set("safety", "vendor/safety-picked")
    schema = JSONSchema(name="x", schema={"type": "object"}, strict=True)

    await provider.complete(
        [LLMMessage(role="user", content="?")], conversation_id="x", json_schema=schema
    )
    kwargs = client.chat.completions.kwargs
    assert kwargs["model"] == "vendor/safety-picked"
    assert kwargs["extra_body"]["provider"] == {"data_collection": "deny", "require_parameters": True}
    assert kwargs["response_format"]["json_schema"]["name"] == "x"

    await provider.complete(
        [LLMMessage(role="user", content="?")],
        conversation_id="x",
        web_search=WebSearch(max_results=3),
    )
    kwargs = client.chat.completions.kwargs
    assert kwargs["model"] == "vendor/safety-picked"
    assert kwargs["extra_body"]["plugins"][0]["id"] == "web"
    assert "tools" not in kwargs and "tool_choice" not in kwargs


async def test_a_role_with_no_model_refuses_instead_of_sending_an_empty_one(sessionmaker):
    """Only the shadow can resolve to "" (off). A chat provider bound to
    it must raise before the request, not send `model=""`."""
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    provider, client = _provider(resolver, role="welfare_shadow")
    with pytest.raises(LLMError):
        await _say(provider)
    assert client.chat.completions.models == []


def test_resolver_and_role_come_together():
    resolver = ModelResolver(_settings(), sessionmaker=None)
    with pytest.raises(ValueError):
        _provider(resolver, role=None)
    with pytest.raises(ValueError):
        _provider(None, role="cheap")
    with pytest.raises(ValueError):
        _provider(resolver, role="narrator")


# --- the decisions provider (the welfare shadow) ------------------------------


class _DecisionClient:
    def __init__(self):
        self.models: list[str] = []

    async def post(self, url, *, cast_to, body):
        self.models.append(body["model"])
        return {
            "answers": {"q": {"choice": "a", "confidence": 0.9, "probabilities": {"a": 0.9, "b": 0.1}}},
            "usage": {"input_tokens": 3, "output_tokens": 0},
        }


_QUESTIONS = {"q": Choice(instructions="?", criteria={"a": "A", "b": "B"})}


async def test_decider_resolves_and_refuses_when_off(sessionmaker):
    resolver = await ModelResolver.create(
        _settings(WELFARE_SHADOW_MODEL="typesafe/jev-1.14"), sessionmaker
    )
    client = _DecisionClient()
    decider = OpenRouterDecisionProvider(
        "typesafe/jev-1.14", "deny", client, resolver=resolver, role="welfare_shadow"
    )
    assert decider.enabled
    assert (await decider.decide("s", _QUESTIONS)).model == "typesafe/jev-1.14"

    await resolver.set("welfare_shadow", "vendor/other-decider")
    assert (await decider.decide("s", _QUESTIONS)).model == "vendor/other-decider"

    await resolver.set("welfare_shadow", "")
    assert not decider.enabled
    with pytest.raises(LLMError):
        await decider.decide("s", _QUESTIONS)
    assert client.models == ["typesafe/jev-1.14", "vendor/other-decider"]


async def test_decider_off_in_env_turns_on_from_the_menu(sessionmaker):
    """build_welfare_decider is always built once resolvers are wired:
    env off, then a menu pick, and the shadow starts asking."""
    resolver = await ModelResolver.create(_settings(), sessionmaker)
    decider = OpenRouterDecisionProvider(
        "", "deny", _DecisionClient(), resolver=resolver, role="welfare_shadow"
    )
    assert not decider.enabled
    await resolver.set("welfare_shadow", "typesafe/jev-1.14")
    assert decider.enabled and decider.model == "typesafe/jev-1.14"


# --- spend bills an overridden model at its stored prices ---------------------

_USAGE = LLMUsage(input_tokens=1_000_000, cached_tokens=200_000, output_tokens=1_000_000, cost_usd=None)


async def test_overridden_model_bills_at_its_stored_prices(sessionmaker, uninstall):
    settings = _settings(LLM_PRICE_IN=1.0, LLM_PRICE_CACHED=0.5, LLM_PRICE_OUT=2.0)
    resolver = await ModelResolver.create(settings, sessionmaker)
    await resolver.set(
        "cheap",
        "vendor/new-cheap",
        ModelPrices(decimal.Decimal("0.10"), decimal.Decimal("0.01"), decimal.Decimal("0.40")),
    )

    # Not installed: nothing changes, the unknown model bills at main prices.
    before = spend.priced(_USAGE, settings, model="vendor/new-cheap")
    assert before.usd == decimal.Decimal("0.8") + decimal.Decimal("0.1") + decimal.Decimal(2)

    llm_models.install(resolver)
    after = spend.priced(_USAGE, settings, model="vendor/new-cheap")
    assert after.source == spend.COMPUTED
    assert after.usd == decimal.Decimal("0.080000") + decimal.Decimal("0.002") + decimal.Decimal("0.4")
    # A model with no override row keeps its env price.
    assert spend.price_triple_for("vendor/persona-env", settings) == (
        decimal.Decimal("1.0"), decimal.Decimal("0.5"), decimal.Decimal("2.0")
    )
    # The vendor's own figure still wins.
    vendor = LLMUsage(input_tokens=1, cached_tokens=0, output_tokens=1, cost_usd=decimal.Decimal("0.123"))
    assert spend.priced(vendor, settings, model="vendor/new-cheap") == spend.Priced(
        decimal.Decimal("0.123000"), spend.VENDOR
    )

    # After a reset the row and its prices are gone.
    await resolver.reset("cheap")
    assert spend.priced(_USAGE, settings, model="vendor/new-cheap").usd == before.usd


async def test_override_without_usable_prices_falls_through(sessionmaker, uninstall):
    settings = _settings(LLM_CHEAP_PRICE_IN=0.2, LLM_CHEAP_PRICE_CACHED=0.1, LLM_CHEAP_PRICE_OUT=0.3)
    resolver = await ModelResolver.create(settings, sessionmaker)
    llm_models.install(resolver)
    # Picked while OpenRouter was unreachable: no prices at all.
    await resolver.set("persona", "vendor/cheap-env")
    assert spend.price_triple_for("vendor/cheap-env", settings) == (
        decimal.Decimal("0.2"), decimal.Decimal("0.1"), decimal.Decimal("0.3")
    )
    # Only an output price is not enough either.
    await resolver.set("persona", "vendor/cheap-env", ModelPrices(None, None, decimal.Decimal(9)))
    assert spend.price_triple_for("vendor/cheap-env", settings)[2] == decimal.Decimal("0.3")
    # No cached price: cached tokens bill at the input price.
    await resolver.set(
        "persona", "vendor/cheap-env", ModelPrices(decimal.Decimal(4), None, decimal.Decimal(8))
    )
    assert spend.price_triple_for("vendor/cheap-env", settings) == (
        decimal.Decimal(4), decimal.Decimal(4), decimal.Decimal(8)
    )


async def test_two_roles_on_one_model_bill_at_the_freshest_usable_quote(sessionmaker, uninstall):
    settings = _settings(LLM_PRICE_IN=1.0, LLM_PRICE_CACHED=0.5, LLM_PRICE_OUT=2.0)
    resolver = await ModelResolver.create(settings, sessionmaker)
    llm_models.install(resolver)
    model = "vendor/shared"
    older = ModelPrices(decimal.Decimal("0.10"), None, decimal.Decimal("0.20"))
    newer = ModelPrices(decimal.Decimal("0.30"), decimal.Decimal("0.03"), decimal.Decimal("0.60"))

    await resolver.set("cheap", model, older)
    await resolver.set("garden", model, newer)
    assert spend.price_triple_for(model, settings) == (
        decimal.Decimal("0.30"), decimal.Decimal("0.03"), decimal.Decimal("0.60")
    )

    # A later pick with no quote (OpenRouter was down) must not hide the
    # earlier one: it is skipped, not preferred.
    await resolver.set("suggest", model)
    assert spend.price_triple_for(model, settings)[0] == decimal.Decimal("0.30")

    # Reset the fresh one: the older quote is what is left.
    await resolver.reset("garden")
    assert spend.price_triple_for(model, settings) == (
        decimal.Decimal("0.10"), decimal.Decimal("0.10"), decimal.Decimal("0.20")
    )


async def test_override_prices_beat_the_env_price_of_a_model_env_also_names(sessionmaker, uninstall):
    """A role switched to a model env prices itself (here the cheap one)
    bills at the catalog figures it was picked with, and goes back to
    env's the moment the row is gone -- by reset or by /delete."""
    settings = _settings(LLM_CHEAP_PRICE_IN=0.2, LLM_CHEAP_PRICE_CACHED=0.1, LLM_CHEAP_PRICE_OUT=0.3)
    resolver = await ModelResolver.create(settings, sessionmaker)
    llm_models.install(resolver)
    await resolver.set(
        "persona",
        "vendor/cheap-env",
        ModelPrices(decimal.Decimal(5), decimal.Decimal(0), decimal.Decimal(9)),
    )
    # An explicit zero cached price is a price, not a missing one.
    assert spend.price_triple_for("vendor/cheap-env", settings) == (
        decimal.Decimal(5), decimal.Decimal(0), decimal.Decimal(9)
    )
    resolver.clear()  # what /delete does
    assert spend.price_triple_for("vendor/cheap-env", settings) == (
        decimal.Decimal("0.2"), decimal.Decimal("0.1"), decimal.Decimal("0.3")
    )
    assert spend.price_triple_for(None, settings) == spend.price_triple_for(None, _settings())
