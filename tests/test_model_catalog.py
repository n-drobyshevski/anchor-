"""Runtime model choice: app/llm/catalog.py's OpenRouter lookups and the
checks a pick must pass before its confirm card (docs/decisions.md,
"Runtime model choice").

Everything talks to `FakeClient`, which answers the two raw GETs and the
Decisions POST from canned bodies shaped like OpenRouter's documented
ones; nothing reaches the network. The resolver is never loaded, so it
resolves every role to env and no database is needed.
"""

from __future__ import annotations

import decimal

import httpx2 as httpx
import openai
import pytest

from app.config import Settings
from app.llm import catalog as catalog_module
from app.llm.catalog import (
    REFUSALS,
    ROLE_NEEDS,
    CatalogUnreachable,
    ModelCatalog,
    choices_for,
    per_million,
    validate_choice,
)
from app.llm.models import NO_PRICES, ROLES, ModelPrices, ModelResolver
from app.llm.openrouter import DECISIONS_URL

ENV = {
    "LLM_MODEL": "vendor/persona-env",
    "LLM_MODEL_CHEAP": "vendor/cheap-env",
    "LLM_MODEL_SAFETY": "vendor/safety-env",
    "LLM_MODEL_JUDGE": "vendor/judge-env",
    "MODEL_CHOICES": (
        "vendor/persona-env|Persona|prose;"
        "vendor/judge-env|Judge|json;"
        "vendor/jsonish|JSONish|json;"
        "typesafe/jev-1.14|Jev|decisions"
    ),
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**ENV, **overrides})


def _endpoint(model_id: str, tag: str, *, params=("temperature", "structured_outputs"),
              prompt="0.0000003", completion="0.0000005", cached="0.00000015") -> dict:
    pricing = {"prompt": prompt, "completion": completion, "request": "0", "image": "0"}
    if cached is not None:
        pricing["input_cache_read"] = cached
    return {
        "model_id": model_id,
        "name": f"{tag}: {model_id}",
        "provider_name": tag.title(),
        "tag": tag,
        "pricing": pricing,
        "supported_parameters": list(params),
        "status": 0,
    }


def _listing(model_id: str, *endpoints: dict) -> dict:
    return {"data": {"id": model_id, "name": model_id, "endpoints": list(endpoints)}}


def _status_error(cls, status: int, method: str = "GET"):
    request = httpx.Request(method, "https://openrouter.ai/api/v1/x")
    return cls("status", response=httpx.Response(status, request=request), body=None)


class FakeClient:
    """Stands in for AsyncOpenAI: `get` answers from `routes` (a body or
    an exception to raise), `post` from `decisions`."""

    def __init__(self, routes: dict | None = None, decisions: dict | None = None) -> None:
        self.routes = dict(routes or {})
        self.decisions = dict(decisions or {})
        self.gets: list[str] = []
        self.posts: list[dict] = []

    async def get(self, path, *, cast_to):
        assert cast_to is object
        self.gets.append(path)
        answer = self.routes.get(path)
        if answer is None:
            raise _status_error(openai.NotFoundError, 404)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def post(self, url, *, cast_to, body):
        assert url == DECISIONS_URL and cast_to is object
        self.posts.append(body)
        answer = self.decisions.get(body["model"])
        if answer is None:
            raise _status_error(openai.BadRequestError, 400, "POST")
        if isinstance(answer, BaseException):
            raise answer
        return answer


ZDR_PATH = "/endpoints/zdr"


def _path(model_id: str) -> str:
    return f"/models/{model_id}/endpoints"


def _catalog(client: FakeClient, settings: Settings | None = None, now=None) -> ModelCatalog:
    settings = settings or _settings()
    if now is None:
        return ModelCatalog(client, settings)
    return ModelCatalog(client, settings, now=now)


def _resolver(settings: Settings) -> ModelResolver:
    # Never loaded: every role resolves to env, and nothing touches a DB.
    return ModelResolver(settings, sessionmaker=None)


GOOD = "vendor/good-model"
GOOD_ZDR = _endpoint(GOOD, "zdrhost")
GOOD_LEAKY = _endpoint(GOOD, "leaky", prompt="0.000001", completion="0.000002")


def _good_client(**routes) -> FakeClient:
    return FakeClient(
        {
            _path(GOOD): _listing(GOOD, GOOD_ZDR, GOOD_LEAKY),
            ZDR_PATH: {"data": [GOOD_ZDR]},
            **routes,
        }
    )


async def _check(role, model_id, client, settings=None):
    settings = settings or _settings()
    return await validate_choice(
        role, model_id, _resolver(settings), settings, _catalog(client, settings)
    )


def test_role_needs_cover_every_role_and_refusals_are_closed():
    assert set(ROLE_NEEDS) == set(ROLES)
    assert set(REFUSALS) == {
        "missing",
        "no_structured",
        "no_deny",
        "judge_equals_persona",
        "not_decisions",
        "not_prose",
        "unreachable",
    }
    assert REFUSALS["unreachable"] == "Не удалось проверить модель, попробуй позже."


def test_per_million_converts_and_rejects_junk():
    assert per_million("0.0000003") == decimal.Decimal("0.300000")
    assert per_million("0.000000075") == decimal.Decimal("0.075000")
    assert per_million("0") == decimal.Decimal(0)
    for junk in (None, "-1", "abc", True, "NaN", "inf"):
        assert per_million(junk) is None


async def test_fetch_model_parses_prices_parameters_and_zdr():
    client = _good_client()
    model = await _catalog(client).fetch_model(GOOD)
    assert model is not None and model.id == GOOD
    assert model.deny_compliant
    assert {e.provider: e.zdr for e in model.endpoints} == {"zdrhost": True, "leaky": False}
    assert model.supported_parameters == {"temperature", "structured_outputs"}
    zdr_only = model.routable(deny=True)
    assert len(zdr_only) == 1
    assert zdr_only[0].prices == ModelPrices(
        decimal.Decimal("0.3"), decimal.Decimal("0.15"), decimal.Decimal("0.5")
    )
    # Across every endpoint the highest price of each kind is kept.
    assert model.prices == ModelPrices(
        decimal.Decimal(1), decimal.Decimal("0.15"), decimal.Decimal(2)
    )


async def test_fetch_model_caches_for_the_ttl_and_not_failures():
    clock = [0.0]
    client = _good_client()
    settings = _settings(MODEL_CATALOG_TTL_H=1)
    catalog = _catalog(client, settings, now=lambda: clock[0])
    await catalog.fetch_model(GOOD)
    await catalog.fetch_model(GOOD)
    assert client.gets == [_path(GOOD), ZDR_PATH]
    clock[0] = 3601.0
    await catalog.fetch_model(GOOD)
    assert client.gets == [_path(GOOD), ZDR_PATH, _path(GOOD), ZDR_PATH]

    # A missing model is asked again next time, not remembered.
    assert await catalog.fetch_model("vendor/nope") is None
    assert await catalog.fetch_model("vendor/nope") is None
    assert client.gets.count(_path("vendor/nope")) == 2


async def test_fetch_model_never_sends_a_non_id():
    client = _good_client()
    catalog = _catalog(client)
    for junk in ("../../keys", "no-slash", "vendor/../x/y", ""):
        assert await catalog.fetch_model(junk) is None
    assert client.gets == []


async def test_unreachable_raises_and_backs_off():
    clock = [0.0]
    client = _good_client(**{_path(GOOD): openai.APIConnectionError(request=httpx.Request(
        "GET", "https://openrouter.ai/api/v1/x"))})
    catalog = _catalog(client, now=lambda: clock[0])
    with pytest.raises(CatalogUnreachable):
        await catalog.fetch_model(GOOD)
    with pytest.raises(CatalogUnreachable):
        await catalog.fetch_model(GOOD)
    assert client.gets == [_path(GOOD)]  # the second call did not ask
    assert await catalog.prices(GOOD) == NO_PRICES  # display never raises
    clock[0] = catalog_module.UNREACHABLE_BACKOFF_SECONDS + 1
    client.routes[_path(GOOD)] = _listing(GOOD, GOOD_ZDR)
    assert (await catalog.fetch_model(GOOD)) is not None


async def test_server_error_and_malformed_body_are_unreachable():
    for answer in (_status_error(openai.InternalServerError, 503), {"data": "nope"}):
        client = _good_client(**{_path(GOOD): answer})
        with pytest.raises(CatalogUnreachable):
            await _catalog(client).fetch_model(GOOD)


# --- validate_choice: success paths ---------------------------------------------


async def test_custom_json_model_passes_with_compliant_prices():
    verdict = await _check("safety", GOOD, _good_client())
    assert verdict.ok and verdict.message is None and verdict.curated is None
    assert verdict.model == GOOD
    # Priced from the ZDR endpoint only: the leaky one can't be routed to.
    assert verdict.prices == ModelPrices(
        decimal.Decimal("0.3"), decimal.Decimal("0.15"), decimal.Decimal("0.5")
    )


async def test_custom_prose_model_passes_without_structured_outputs():
    plain = _endpoint(GOOD, "zdrhost", params=("temperature",))
    client = FakeClient({_path(GOOD): _listing(GOOD, plain), ZDR_PATH: {"data": [plain]}})
    assert (await _check("cheap", GOOD, client)).ok


async def test_curated_pick_takes_catalog_prices():
    client = FakeClient(
        {
            _path("vendor/jsonish"): _listing("vendor/jsonish", _endpoint("vendor/jsonish", "a")),
            ZDR_PATH: {"data": [_endpoint("vendor/jsonish", "a")]},
        }
    )
    verdict = await _check("garden", "vendor/jsonish", client)
    assert verdict.ok and verdict.curated is not None and verdict.curated.label == "JSONish"
    assert verdict.prices.price_in == decimal.Decimal("0.3")


async def test_curated_pick_survives_unreachable_with_empty_prices():
    error = _status_error(openai.InternalServerError, 502)
    client = FakeClient({_path("vendor/jsonish"): error})
    verdict = await _check("suggest", "vendor/jsonish", client)
    assert verdict.ok and verdict.prices == NO_PRICES


async def test_welfare_shadow_may_be_switched_off_and_takes_curated_decisions():
    client = FakeClient()
    assert (await _check("welfare_shadow", "", client)).ok
    jev = await _check("welfare_shadow", "typesafe/jev-1.14", client)
    assert jev.ok and jev.prices == NO_PRICES
    assert client.posts == []  # a curated decisions model is not probed


async def test_custom_decision_model_passes_after_a_probe():
    probe_body = {
        "answers": {"probe": {"choice": "ok", "confidence": 0.9,
                               "probabilities": {"ok": 0.9, "no": 0.1}}},
        "usage": {"input_tokens": 12, "output_tokens": 0, "cost": 0.00001},
    }
    client = FakeClient(decisions={"vendor/decider": probe_body})
    verdict = await _check("welfare_shadow", "vendor/decider", client)
    assert verdict.ok and verdict.probe_usage is not None
    assert verdict.probe_usage.input_tokens == 12
    (body,) = client.posts
    assert body["provider"] == {"data_collection": "deny", "zdr": True}
    assert body["state"] == catalog_module._PROBE_STATE


async def test_data_collection_allow_skips_the_zdr_requirement():
    leaky = _endpoint(GOOD, "leaky")
    client = FakeClient({_path(GOOD): _listing(GOOD, leaky), ZDR_PATH: {"data": []}})
    verdict = await _check("safety", GOOD, client, _settings(LLM_DATA_COLLECTION="allow"))
    assert verdict.ok


# --- validate_choice: each refusal ----------------------------------------------


async def test_refuses_missing_model():
    for model_id in ("vendor/does-not-exist", "not an id", "  "):
        verdict = await _check("safety", model_id, _good_client())
        assert verdict.code == "missing" and not verdict.ok
        assert verdict.message == REFUSALS["missing"]


async def test_refuses_model_with_no_endpoints_as_missing():
    client = _good_client(**{_path("vendor/empty"): _listing("vendor/empty")})
    assert (await _check("persona", "vendor/empty", client)).code == "missing"


async def test_refuses_json_role_without_structured_outputs():
    # structured_outputs exists, but only on the endpoint deny can't use.
    zdr = _endpoint(GOOD, "zdrhost", params=("temperature",))
    leaky = _endpoint(GOOD, "leaky")
    client = FakeClient({_path(GOOD): _listing(GOOD, zdr, leaky), ZDR_PATH: {"data": [zdr]}})
    assert (await _check("judge", GOOD, client)).code == "no_structured"
    # And no temperature is as bad as no schema.
    zdr = _endpoint(GOOD, "zdrhost", params=("structured_outputs",))
    client = FakeClient({_path(GOOD): _listing(GOOD, zdr), ZDR_PATH: {"data": [zdr]}})
    assert (await _check("safety", GOOD, client)).code == "no_structured"


async def test_refuses_curated_entry_without_the_needed_cap():
    client = FakeClient()
    assert (await _check("safety", "vendor/persona-env", client)).code == "no_structured"
    assert (await _check("persona", "vendor/jsonish", client)).code == "not_prose"
    assert (await _check("welfare_shadow", "vendor/jsonish", client)).code == "not_decisions"
    assert client.gets == []


async def test_refuses_model_with_no_deny_compliant_endpoint():
    client = _good_client(**{ZDR_PATH: {"data": [_endpoint("vendor/other", "zdrhost")]}})
    verdict = await _check("persona", GOOD, client)
    assert verdict.code == "no_deny"


async def test_refuses_judge_equal_to_persona_both_ways():
    client = _good_client()
    settings = _settings()
    assert (await _check("judge", settings.LLM_MODEL, client)).code == "judge_equals_persona"
    assert (await _check("persona", settings.LLM_MODEL_JUDGE, client)).code == (
        "judge_equals_persona"
    )
    assert client.gets == []  # decided before any lookup


async def test_refuses_custom_non_decision_model_for_the_shadow():
    client = _good_client()  # GOOD exists, but the Decisions API refuses it
    verdict = await _check("welfare_shadow", GOOD, client)
    assert verdict.code == "not_decisions"
    assert len(client.posts) == 1
    # An id OpenRouter does not know at all is reported as missing.
    assert (await _check("welfare_shadow", "vendor/ghost", client)).code == "missing"


async def test_refuses_custom_id_when_unreachable():
    down = openai.APIConnectionError(request=httpx.Request("GET", "https://openrouter.ai"))
    client = FakeClient({_path(GOOD): down})
    verdict = await _check("safety", GOOD, client)
    assert verdict.code == "unreachable"
    assert verdict.message == "Не удалось проверить модель, попробуй позже."

    # The Decisions probe failing transiently is unreachable too.
    flaky = _status_error(openai.InternalServerError, 503, "POST")
    client = FakeClient(decisions={"vendor/decider": flaky})
    assert (await _check("welfare_shadow", "vendor/decider", client)).code == "unreachable"


def test_choices_for_filters_by_caps():
    settings = _settings()
    assert [c.id for c in choices_for("persona", settings)] == ["vendor/persona-env"]
    assert [c.id for c in choices_for("safety", settings)] == [
        "vendor/judge-env",
        "vendor/jsonish",
    ]
    assert [c.id for c in choices_for("welfare_shadow", settings)] == ["typesafe/jev-1.14"]
