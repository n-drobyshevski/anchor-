"""What OpenRouter says about a model, and whether a role may run on it
(docs/decisions.md, "Runtime model choice").

⚙️ Настройки › 🧠 Модели offers every role a curated list
(`MODEL_CHOICES`, app/config.py) plus ✏️ «Своя модель», a free-form id.
Before the confirm card is shown, `validate_choice` decides whether the
pick is allowed and with which prices; app/llm/models.py's
`ModelResolver.set` then only stores what it was handed. Nothing here
writes anything.

**Where the facts come from.** `ModelCatalog` asks OpenRouter through
the chat providers' own `AsyncOpenAI` client (the same key and
connection pool; app/llm/openrouter.py's `build_client`), with the SDK's
raw `client.get(path, cast_to=object)`, the same pattern
`OpenRouterDecisionProvider` uses for the Decisions URL:

- `GET /models/{id}/endpoints`: does the model exist, and per endpoint
  (one provider serving it) its prices and `supported_parameters`;
- `GET /endpoints/zdr`: every endpoint that keeps no prompts at all.

Both are cached for `MODEL_CATALOG_TTL_H` hours; a failure is never
cached as an answer, only as a short back-off, so a menu render during
an outage does not wait on OpenRouter once per button.

**Deny compliance is read as ZDR.** The bot sends
`data_collection=deny` (LLM_DATA_COLLECTION) on every request, and
OpenRouter refuses a request that no compliant endpoint can serve. The
public endpoint listing carries no data-policy field, though, so the
only published list of endpoints that certainly comply is the ZDR one
(zero retention is strictly more than "does not collect"). A custom id
must have at least one endpoint there. That is conservative: a model
whose providers comply with deny without being ZDR is refused here
(add it to MODEL_CHOICES instead), never the other way round. And it is
a usability check, not the privacy guarantee -- that stays the
`data_collection` field each request carries, which a wrong answer
here cannot remove; the worst a wrong "yes" costs is failed calls.

**The checks** (`validate_choice`), each a code from `REFUSALS`:

- `missing`: OpenRouter does not know the id (or it is not an id);
- `no_structured`: a JSON role (`safety`, `judge`, `garden`,
  `suggest`) needs `structured_outputs` and `temperature` on one and
  the same compliant endpoint, because those calls send
  `require_parameters` and a schema (app/llm/openrouter.py);
- `no_deny`: no compliant endpoint, as above;
- `judge_equals_persona`: the judge would grade the persona's own
  replies (app/core/idle/gate.py's independence rule) -- checked both
  ways, so switching the chat onto the judge's model is refused too;
- `not_decisions`: the welfare shadow runs on the Decisions API, which
  no listing describes, so a custom id is probed with one synthetic
  decision (no user text) and refused if that fails;
- `not_prose`: a curated entry whose caps do not include `prose`,
  picked for the chat or summary role;
- `unreachable`: OpenRouter could not be asked. A *curated* pick still
  goes through, with its prices left empty (spend then bills it at the
  env triples, app/core/spend.py); a custom id is refused, because
  nothing about it could be checked.

Curated entries are the owner's own list, so they are not checked
against the catalog at all beyond their caps and the judge rule -- the
catalog only supplies their prices. A stale entry costs failed calls,
not a wrong answer, and the Decisions API's models may well not appear
in the chat listing.

**Prices** come as USD per token strings; they are converted to USD per
million (llm_model_override's unit) and, across the endpoints the role
could be routed to, the highest of each is kept: OpenRouter picks among
them, and the estimate on a confirm card should not be the cheapest
case. The cost OpenRouter reports for a call still wins in the ledger.

Logs carry `event` and `error_code` (an exception class name) only --
no model id, no response body.
"""

from __future__ import annotations

import asyncio
import decimal
import logging
import time
from collections.abc import Callable, Iterable
from typing import NamedTuple

import openai
from openai import AsyncOpenAI

from app.config import _MODEL_ID_RE, ModelChoice, Settings, model_choices
from app.llm.models import NO_PRICES, OFF_ROLE, ROLES, ModelPrices, ModelResolver
from app.llm.openrouter import OpenRouterDecisionProvider
from app.llm.provider import Choice, LLMError, LLMRetryableError, LLMUsage

logger = logging.getLogger(__name__)

# What each role needs a model to do: a MODEL_CAPS entry (app/config.py).
ROLE_NEEDS: dict[str, str] = {
    "persona": "prose",
    "cheap": "prose",
    "safety": "json",
    "judge": "json",
    "garden": "json",
    "suggest": "json",
    "welfare_shadow": "decisions",
}

# The parameters a JSON role's request depends on (app/llm/openrouter.py
# sends both, with require_parameters, on every json_schema call).
JSON_PARAMETERS = frozenset({"structured_outputs", "temperature"})

# The closed set of refusals, with what the menu says for each. The
# texts name no model: the card above them already does.
REFUSALS: dict[str, str] = {
    "missing": "Такой модели в OpenRouter нет. Проверь id.",
    "no_structured": (
        "Эта модель не умеет строгий JSON с temperature, а этой роли он нужен."
    ),
    "no_deny": (
        "У этой модели нет провайдера, который не хранит запросы, — "
        "с data_collection=deny она не ответит."
    ),
    "judge_equals_persona": (
        "Судья и чат не могут быть одной моделью: судья оценивал бы сам себя."
    ),
    "not_decisions": "Это не модель Decisions API — Welfare-тени она не подойдёт.",
    "not_prose": "Эта модель в списке не для текста — для этой роли не подойдёт.",
    "unreachable": "Не удалось проверить модель, попробуй позже.",
}

# A menu render must never hang on OpenRouter: the client's own timeout
# (app/llm/openrouter.py, 90 s) is a chat turn's, not a button's.
CATALOG_TIMEOUT_SECONDS = 10.0
# After a failure, how long to answer "unreachable" without asking again.
UNREACHABLE_BACKOFF_SECONDS = 60.0

MODELS_ENDPOINTS_PATH = "/models/{model_id}/endpoints"
ZDR_ENDPOINTS_PATH = "/endpoints/zdr"

_PER_MILLION = decimal.Decimal(1_000_000)
_PRICE_EXPONENT = decimal.Decimal("0.000001")
# The synthetic decision a custom welfare-shadow id is probed with. It
# carries no user text; output tokens are free on the Decisions API.
_PROBE_STATE = "Проверка связи."
_PROBE_QUESTION = {
    "probe": Choice(
        instructions="Ответь «ok».",
        criteria={"ok": "связь есть", "no": "связи нет"},
    )
}


class CatalogUnreachable(Exception):
    """OpenRouter could not be asked (network, timeout, 5xx, auth, or a
    body this module cannot read). Carries no response text."""


class Endpoint(NamedTuple):
    """One provider serving a model, as `GET /models/{id}/endpoints`
    lists it."""

    provider: str
    supported_parameters: frozenset[str]
    prices: ModelPrices
    # Listed by `GET /endpoints/zdr`: keeps no prompts, so it complies
    # with data_collection=deny.
    zdr: bool


class CatalogModel(NamedTuple):
    """What OpenRouter says about one model id."""

    id: str
    endpoints: tuple[Endpoint, ...]

    @property
    def deny_compliant(self) -> bool:
        """Some endpoint certainly complies with data_collection=deny."""
        return any(endpoint.zdr for endpoint in self.endpoints)

    @property
    def supported_parameters(self) -> frozenset[str]:
        """Every parameter some endpoint accepts (the model-level union;
        a single endpoint may accept fewer)."""
        return frozenset().union(*(e.supported_parameters for e in self.endpoints))

    def routable(self, *, deny: bool, parameters: Iterable[str] = ()) -> tuple[Endpoint, ...]:
        """The endpoints a request could be routed to: compliant ones
        when `deny`, and only those accepting every one of `parameters`
        (what require_parameters asks OpenRouter to enforce)."""
        needed = frozenset(parameters)
        return tuple(
            endpoint
            for endpoint in self.endpoints
            if (endpoint.zdr or not deny) and needed <= endpoint.supported_parameters
        )

    @property
    def prices(self) -> ModelPrices:
        """The highest prices across all endpoints (see `highest_prices`)."""
        return highest_prices(self.endpoints)


class Probe(NamedTuple):
    """`ModelCatalog.probe_decisions`'s answer. `usage` is None when no
    call was made this time (a success remembered from earlier, or a
    refusal), so a caller ledgers only calls that happened."""

    answered: bool
    usage: LLMUsage | None = None


class Verdict(NamedTuple):
    """`validate_choice`'s answer.

    `code` is None when the pick is allowed, else a key of `REFUSALS`.
    `prices` are what the confirm card shows and `ModelResolver.set`
    stores (NO_PRICES when unknown). `probe_usage` is the usage of the
    Decisions probe when one was made, for the caller to ledger.
    """

    model: str
    code: str | None
    prices: ModelPrices = NO_PRICES
    curated: ModelChoice | None = None
    probe_usage: LLMUsage | None = None

    @property
    def ok(self) -> bool:
        return self.code is None

    @property
    def message(self) -> str | None:
        return REFUSALS[self.code] if self.code is not None else None


def _refuse(model: str, code: str, curated: ModelChoice | None = None) -> Verdict:
    assert code in REFUSALS, code
    return Verdict(model, code, NO_PRICES, curated)


# --- parsing OpenRouter's JSON ------------------------------------------------


def per_million(value) -> decimal.Decimal | None:
    """An OpenRouter per-token price ("0.0000003") as USD per million
    tokens (Decimal("0.3")), or None when absent, unreadable or negative
    (OpenRouter's router models list "-1" for "varies")."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = decimal.Decimal(str(value))
    except decimal.InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    return (number * _PER_MILLION).quantize(_PRICE_EXPONENT, rounding=decimal.ROUND_HALF_UP)


def _endpoint_prices(pricing) -> ModelPrices:
    pricing = pricing if isinstance(pricing, dict) else {}
    return ModelPrices(
        per_million(pricing.get("prompt")),
        per_million(pricing.get("input_cache_read")),
        per_million(pricing.get("completion")),
    )


def highest_prices(endpoints: Iterable[Endpoint]) -> ModelPrices:
    """Per field, the highest price any of `endpoints` lists; None where
    none lists one."""
    endpoints = tuple(endpoints)

    def highest(field: int) -> decimal.Decimal | None:
        known = [e.prices[field] for e in endpoints if e.prices[field] is not None]
        return max(known) if known else None

    return ModelPrices(highest(0), highest(1), highest(2))


def _endpoint_key(raw: dict, fallback_model: str) -> tuple[str, str]:
    """What identifies one endpoint across the two listings: the model
    id the listing itself gives (both use OpenRouter's PublicEndpoint
    shape) and the provider's tag, else its name."""
    model = raw.get("model_id") if isinstance(raw.get("model_id"), str) else fallback_model
    provider = raw.get("tag") or raw.get("provider_name") or raw.get("name") or ""
    return (model, str(provider))


def _parameters(raw) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(item for item in raw if isinstance(item, str))


def parse_endpoints(
    model_id: str, payload, zdr_keys: frozenset[tuple[str, str]]
) -> CatalogModel | None:
    """`GET /models/{id}/endpoints`'s body as a CatalogModel; None when
    it lists no endpoint (a model nothing serves is as good as missing).
    Raises CatalogUnreachable on a body that is not the documented
    shape: that is "could not check", not "does not exist"."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("endpoints", []), list):
        raise CatalogUnreachable("malformed endpoints listing")
    endpoints = []
    for raw in data.get("endpoints") or []:
        if not isinstance(raw, dict):
            continue
        key = _endpoint_key(raw, model_id)
        endpoints.append(
            Endpoint(
                provider=key[1],
                supported_parameters=_parameters(raw.get("supported_parameters")),
                prices=_endpoint_prices(raw.get("pricing")),
                zdr=key in zdr_keys,
            )
        )
    if not endpoints:
        return None
    return CatalogModel(model_id, tuple(endpoints))


def parse_zdr(payload) -> frozenset[tuple[str, str]]:
    """`GET /endpoints/zdr`'s body as the set of endpoint keys."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise CatalogUnreachable("malformed zdr listing")
    return frozenset(
        _endpoint_key(raw, "")
        for raw in data
        if isinstance(raw, dict) and isinstance(raw.get("model_id"), str)
    )


# --- the catalog ---------------------------------------------------------------


class ModelCatalog:
    """OpenRouter's model facts, cached.

    Built once in app/main.py from the shared `AsyncOpenAI` client and
    Settings; `now` is injectable so tests can age the cache.
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._settings = settings
        self._now = now
        self._ttl = settings.MODEL_CATALOG_TTL_H * 3600.0
        self._models: dict[str, tuple[float, CatalogModel]] = {}
        self._zdr: tuple[float, frozenset[tuple[str, str]]] | None = None
        self._decisions: dict[str, float] = {}
        self._down_until = 0.0

    def clear(self) -> None:
        """Forget everything cached, the back-off included."""
        self._models.clear()
        self._zdr = None
        self._decisions.clear()
        self._down_until = 0.0

    async def _get(self, path: str):
        """One raw GET under CATALOG_TIMEOUT_SECONDS. Returns the parsed
        JSON body, or None for a 400/404/422 (the id names nothing).
        Anything else is CatalogUnreachable, which also starts the
        back-off."""
        if self._now() < self._down_until:
            raise CatalogUnreachable("backing off")
        try:
            async with asyncio.timeout(CATALOG_TIMEOUT_SECONDS):
                return await self._client.get(path, cast_to=object)
        except (openai.NotFoundError, openai.BadRequestError, openai.UnprocessableEntityError):
            return None
        except Exception as exc:  # noqa: BLE001 - any other failure means "could not check"
            self._unreachable(exc)
            raise CatalogUnreachable(type(exc).__name__) from None

    def _unreachable(self, exc: BaseException) -> None:
        self._down_until = self._now() + UNREACHABLE_BACKOFF_SECONDS
        logger.warning(
            "model catalog unreachable",
            extra={"event": "model_catalog_unreachable", "error_code": type(exc).__name__},
        )

    async def _zdr_keys(self) -> frozenset[tuple[str, str]]:
        if self._zdr is not None and self._zdr[0] > self._now():
            return self._zdr[1]
        payload = await self._get(ZDR_ENDPOINTS_PATH)
        if payload is None:
            raise CatalogUnreachable("no zdr listing")
        try:
            keys = parse_zdr(payload)
        except CatalogUnreachable as exc:
            self._unreachable(exc)
            raise
        self._zdr = (self._now() + self._ttl, keys)
        return keys

    async def fetch_model(self, model_id: str) -> CatalogModel | None:
        """What OpenRouter says about `model_id`: None when it does not
        exist (or is not an id at all), CatalogUnreachable when that
        could not be found out. Only a found model is cached."""
        model_id = model_id.strip()
        if not _MODEL_ID_RE.match(model_id):
            return None
        cached = self._models.get(model_id)
        if cached is not None and cached[0] > self._now():
            return cached[1]
        payload = await self._get(MODELS_ENDPOINTS_PATH.format(model_id=model_id))
        if payload is None:
            return None
        zdr_keys = await self._zdr_keys()
        try:
            model = parse_endpoints(model_id, payload, zdr_keys)
        except CatalogUnreachable as exc:
            self._unreachable(exc)
            raise
        if model is not None:
            self._models[model_id] = (self._now() + self._ttl, model)
        return model

    async def prices(self, model_id: str, *, deny: bool | None = None) -> ModelPrices:
        """Best-effort prices for display (a curated button): the highest
        across the endpoints a request could reach, NO_PRICES when the
        model is unknown or OpenRouter unreachable. Never raises."""
        if deny is None:
            deny = self._deny
        try:
            model = await self.fetch_model(model_id)
        except CatalogUnreachable:
            return NO_PRICES
        if model is None:
            return NO_PRICES
        return highest_prices(model.routable(deny=deny) or model.endpoints)

    @property
    def _deny(self) -> bool:
        return self._settings.LLM_DATA_COLLECTION == "deny"

    async def probe_decisions(self, model_id: str) -> Probe:
        """Ask `model_id` one synthetic decision through the Decisions
        API, exactly as the welfare shadow would (deny and zdr).
        `answered` is False when OpenRouter refused it (not a decision
        model, not served with zdr, not a model at all); a transient
        failure raises CatalogUnreachable. A success is remembered for
        the TTL and not asked again."""
        model_id = model_id.strip()
        if not _MODEL_ID_RE.match(model_id):
            return Probe(False)
        if self._decisions.get(model_id, 0.0) > self._now():
            return Probe(True)
        if self._now() < self._down_until:
            raise CatalogUnreachable("backing off")
        decider = OpenRouterDecisionProvider(
            model_id, self._settings.LLM_DATA_COLLECTION, self._client
        )
        try:
            async with asyncio.timeout(CATALOG_TIMEOUT_SECONDS):
                response = await decider.decide(_PROBE_STATE, _PROBE_QUESTION)
        except LLMError:
            return Probe(False)
        except (LLMRetryableError, TimeoutError) as exc:
            self._unreachable(exc)
            raise CatalogUnreachable(type(exc).__name__) from None
        self._decisions[model_id] = self._now() + self._ttl
        return Probe(True, response.usage)


# --- the checks ----------------------------------------------------------------


def curated_choice(model_id: str, settings: Settings) -> ModelChoice | None:
    """The MODEL_CHOICES entry for `model_id`, if it is one."""
    for choice in model_choices(settings):
        if choice.id == model_id:
            return choice
    return None


def choices_for(role: str, settings: Settings) -> tuple[ModelChoice, ...]:
    """The curated entries a role's screen offers: those whose caps
    include what the role needs, in MODEL_CHOICES order."""
    need = ROLE_NEEDS[role]
    return tuple(choice for choice in model_choices(settings) if need in choice.caps)


_CAP_REFUSAL = {"prose": "not_prose", "json": "no_structured", "decisions": "not_decisions"}


def _independence(role: str, model_id: str, resolver: ModelResolver) -> bool:
    """False when the pick would make the judge and the persona one model."""
    if role == "judge":
        return model_id != resolver.effective("persona")
    if role == "persona":
        return model_id != resolver.effective("judge")
    return True


async def validate_choice(
    role: str,
    model_id: str,
    resolver: ModelResolver,
    settings: Settings,
    catalog: ModelCatalog,
) -> Verdict:
    """May `role` be switched to `model_id`, and at which prices?

    `model_id` may be a curated entry (a button) or a custom id (✏️
    «Своя модель»); which one is decided here by looking it up in
    MODEL_CHOICES, never by the caller. "" is accepted only for
    `welfare_shadow` (switched off), with no lookup. See the module
    docstring for the order and meaning of the checks.
    """
    if role not in ROLES:
        raise ValueError(f"unknown model role {role!r}")
    model_id = model_id.strip()
    if not model_id:
        if role == OFF_ROLE:
            return Verdict("", None)
        return _refuse("", "missing")
    if not _independence(role, model_id, resolver):
        return _refuse(model_id, "judge_equals_persona")

    need = ROLE_NEEDS[role]
    deny = settings.LLM_DATA_COLLECTION == "deny"
    curated = curated_choice(model_id, settings)
    if curated is not None:
        if need not in curated.caps:
            return _refuse(model_id, _CAP_REFUSAL[need], curated)
        parameters = JSON_PARAMETERS if need == "json" else ()
        try:
            model = await catalog.fetch_model(model_id)
        except CatalogUnreachable:
            model = None
        prices = NO_PRICES
        if model is not None:
            prices = highest_prices(
                model.routable(deny=deny, parameters=parameters) or model.endpoints
            )
        return Verdict(model_id, None, prices, curated)

    if not _MODEL_ID_RE.match(model_id):
        return _refuse(model_id, "missing")
    try:
        model = await catalog.fetch_model(model_id)
    except CatalogUnreachable:
        return _refuse(model_id, "unreachable")

    if need == "decisions":
        try:
            probe = await catalog.probe_decisions(model_id)
        except CatalogUnreachable:
            return _refuse(model_id, "unreachable")
        if not probe.answered:
            return _refuse(model_id, "missing" if model is None else "not_decisions")
        # The probe ran with zdr required, so it already proved a
        # compliant endpoint; the listing (if it has the model) only
        # adds prices.
        prices = NO_PRICES
        if model is not None:
            prices = highest_prices(model.routable(deny=True) or model.endpoints)
        return Verdict(model_id, None, prices, None, probe.usage)

    if model is None:
        return _refuse(model_id, "missing")
    compliant = model.routable(deny=deny)
    if not compliant:
        return _refuse(model_id, "no_deny")
    if need == "json":
        compliant = model.routable(deny=deny, parameters=JSON_PARAMETERS)
        if not compliant:
            return _refuse(model_id, "no_structured")
    return Verdict(model_id, None, highest_prices(compliant))
