"""Which OpenRouter model each LLM role runs on, right now
(docs/decisions.md, "Runtime model choice").

Until this module every model came from env (app/config.py's
`LLM_MODEL` and friends) and was fixed into each `OpenRouterProvider`
when it was built, so changing one meant editing env and redeploying.
⚙️ Настройки › 🧠 Модели now lets the owner switch any role from
Telegram, so the model is resolved **per request** instead: a provider
built with `resolver=` and `role=` asks `ModelResolver.effective(role)`
on every `complete()` (app/llm/openrouter.py), and the next call after a
switch already runs on the new model, with no provider rebuilt and no
restart.

**Roles.** `ROLES` is the closed list, in menu order, and it must equal
the `ck_llm_model_override_role` CHECK in
migrations/versions/d8a3f6c1b925_create_llm_model_override.py (and
app/db/models.py's `LlmModelOverride`), which a test ties together:

- `persona` (`LLM_MODEL`): the chat itself, outbound, check-ins, the
  weekly review's prose;
- `cheap` (`LLM_MODEL_CHEAP`): scene summaries and backfill;
- `safety` (`LLM_MODEL_SAFETY`): every strict-JSON verdict (extract,
  welfare, tick, distill, intent, `/task` parsing, the lens query, the
  idle jobs) and the web-search call;
- `judge` (`LLM_MODEL_JUDGE`): critique, canary, the amendment trial;
- `garden` (`GARDEN_MODEL`, "" = the safety model): the lens garden;
- `suggest` (`INTEREST_SUGGEST_MODEL`, "" = the safety model): interest
  suggestions;
- `welfare_shadow` (`WELFARE_SHADOW_MODEL`, "" = off): the shadow
  decider. Its override may be "" too, which means switched off from
  the menu whatever env says.

**Fallback chains** (`effective`). A role's own override row wins; with
none, the role falls back to env -- except `garden` and `suggest`,
which follow the *safety override* before their own env setting, so
that switching «Проверки» moves the two calls that run on the safety
model by default along with it (the menu shows them as «= проверки»):

    garden:  override(garden)  → override(safety) → GARDEN_MODEL           → LLM_MODEL_SAFETY
    suggest: override(suggest) → override(safety) → INTEREST_SUGGEST_MODEL → LLM_MODEL_SAFETY

That is the order the plan approved. It means a deliberately set
`GARDEN_MODEL` yields to a safety switch made in the menu; the garden's
own row (🌱 Сад) is how to pin it apart again.

**Cache.** One process runs the bot, so an in-process dict plus the DB
write is consistent: `set` and `reset` write the row and its audit in
one transaction and update the dict only after the commit, under a
lock, so a failed write never leaves the cache claiming a model the
database does not hold. `load()` fills the dict at startup; until it
has run, every role resolves to env, which is also what an empty table
means. `/delete` truncates the table, and its handler must call
`clear()` after the commit (app/core/purge.py's note) -- otherwise the
cache would keep resolving a model whose row is gone until the next
restart.

**Prices.** An override row carries the three prices OpenRouter's
catalog gave when the model was picked (app/llm/catalog.py), and
app/core/spend.py bills a COMPUTED row for that model at them --
through `override_prices`, which reads the resolver `install()`
registered. A module-level registry rather than a parameter because
`spend.priced` has a score of call sites that already pass only
`(usage, settings, model)`; threading a resolver through each would
touch every ledger writer for a lookup that is the same everywhere.
Nothing is registered unless app/main.py installs the resolver, so a
test that builds one does not change another test's prices.

**Audit.** Every change is a `state_change` row, `field="model:<role>"`,
old and new value the role's *effective* model before and after
("" for the shadow switched off) -- model ids, never content.
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import logging
from typing import NamedTuple

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core import state as state_module
from app.db.models import LlmModelOverride

logger = logging.getLogger(__name__)

# Menu order. Must equal ck_llm_model_override_role.
ROLES: tuple[str, ...] = (
    "persona",
    "cheap",
    "safety",
    "judge",
    "garden",
    "suggest",
    "welfare_shadow",
)

# The env setting each role falls back to (app/config.py). `garden`,
# `suggest` and `welfare_shadow` may be "" there: the first two then
# mean the safety model, the last one "off".
ROLE_SETTINGS: dict[str, str] = {
    "persona": "LLM_MODEL",
    "cheap": "LLM_MODEL_CHEAP",
    "safety": "LLM_MODEL_SAFETY",
    "judge": "LLM_MODEL_JUDGE",
    "garden": "GARDEN_MODEL",
    "suggest": "INTEREST_SUGGEST_MODEL",
    "welfare_shadow": "WELFARE_SHADOW_MODEL",
}

# Roles that run on the safety model when nothing names their own.
SAFETY_FOLLOWERS = frozenset({"garden", "suggest"})

# The only role whose model may be "" ("off"): ck_llm_model_override_model.
OFF_ROLE = "welfare_shadow"

AUDIT_PREFIX = "model:"

# llm_model_override's Numeric(12, 6).
_PRICE_EXPONENT = decimal.Decimal("0.000001")
_PRICE_LIMIT = decimal.Decimal(1_000_000)


class ModelPrices(NamedTuple):
    """USD per million tokens, as OpenRouter's catalog states them.

    Each may be None: a curated pick made while OpenRouter was
    unreachable has none at all, and plenty of models list no cached
    price. app/core/spend.py needs `price_in` and `price_out` to bill at
    these (`usable`); a missing `price_cached` bills cached tokens at
    the input price, which can only over-count.
    """

    price_in: decimal.Decimal | None = None
    price_cached: decimal.Decimal | None = None
    price_out: decimal.Decimal | None = None

    def usable(self) -> bool:
        return self.price_in is not None and self.price_out is not None

    def triple(self) -> tuple[decimal.Decimal, decimal.Decimal, decimal.Decimal]:
        """(input, cached, output) for spend; only when `usable()`."""
        if not self.usable():
            raise ValueError("override prices are incomplete")
        cached = self.price_cached if self.price_cached is not None else self.price_in
        return (self.price_in, cached, self.price_out)


NO_PRICES = ModelPrices()


class Override(NamedTuple):
    """One `llm_model_override` row, as the cache holds it."""

    model: str
    prices: ModelPrices
    updated_at: datetime.datetime | None


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown model role {role!r}; expected one of {', '.join(ROLES)}")


def env_model(settings: Settings, role: str) -> str:
    """What env alone names for `role`, "" in `GARDEN_MODEL` or
    `INTEREST_SUGGEST_MODEL` meaning the safety model. "" only for
    `welfare_shadow` (off). `ModelResolver.env_default` is this, and the
    module-level `effective_model` falls back to it with no resolver
    installed."""
    _check_role(role)
    value = getattr(settings, ROLE_SETTINGS[role])
    if not value and role in SAFETY_FOLLOWERS:
        return settings.LLM_MODEL_SAFETY
    return value


def _price(value) -> decimal.Decimal | None:
    """A stored or offered price as a Decimal, refusing a negative one
    (ck_llm_model_override_price) and float binary noise.

    Quantized to the column's six places (Numeric(12, 6)) here, so the
    cache holds exactly what a restart will read back, and refused at a
    million dollars per million tokens, which the column cannot hold.
    """
    if value is None:
        return None
    number = value if isinstance(value, decimal.Decimal) else decimal.Decimal(str(value))
    if not number.is_finite() or number < 0 or number >= _PRICE_LIMIT:
        raise ValueError("model prices must be finite, >= 0 and < 1e6")
    return number.quantize(_PRICE_EXPONENT, rounding=decimal.ROUND_HALF_UP)


def _prices(prices: ModelPrices | None) -> ModelPrices:
    if prices is None:
        return NO_PRICES
    return ModelPrices(_price(prices.price_in), _price(prices.price_cached), _price(prices.price_out))


class ModelResolver:
    """The effective model per role: override rows over env.

    Built once in app/main.py from Settings and the sessionmaker, then
    `await load()`-ed before the first update is handled (or built with
    `await ModelResolver.create(...)`, which does both). Synchronous
    reads (`effective`, `env_default`, `override`) are what the
    providers call on every request, so they only ever touch the dict;
    the two writes (`set`, `reset`) go to the database first.
    """

    def __init__(self, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._overrides: dict[str, Override] = {}
        self._lock = asyncio.Lock()

    @classmethod
    async def create(
        cls, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> ModelResolver:
        resolver = cls(settings, sessionmaker)
        await resolver.load()
        return resolver

    # --- reads -------------------------------------------------------------

    async def load(self) -> None:
        """Replace the cache with what the table holds now.

        A row whose role is no longer in ROLES (impossible while the
        CHECK holds) is skipped rather than raising: a stale row must
        not stop the bot from booting.
        """
        async with self._sessionmaker() as session:
            rows = (await session.execute(select(LlmModelOverride))).scalars().all()
        overrides = {
            row.role: Override(
                row.model,
                ModelPrices(row.price_in, row.price_cached, row.price_out),
                row.updated_at,
            )
            for row in rows
            if row.role in ROLES
        }
        async with self._lock:
            self._overrides = overrides

    def clear(self) -> None:
        """Forget every override (after `/delete` truncated the table)."""
        self._overrides = {}

    def override(self, role: str) -> Override | None:
        """The role's own row, or None when it runs on its default."""
        _check_role(role)
        return self._overrides.get(role)

    def overridden(self, role: str) -> bool:
        """True while the role has its own row (↩️ «По умолчанию» shows)."""
        return self.override(role) is not None

    def env_default(self, role: str) -> str:
        """What env alone says, ignoring every override: the model the
        role ran on before this module, and what `/state` compares to.
        "" only for `welfare_shadow` (off)."""
        return env_model(self._settings, role)

    def default(self, role: str) -> str:
        """What the role resolves to without its own row -- what ↩️ «По
        умолчанию» would put it back on. Differs from `env_default` only
        for `garden` and `suggest` while safety is overridden."""
        _check_role(role)
        if role in SAFETY_FOLLOWERS:
            safety = self._overrides.get("safety")
            if safety is not None:
                return safety.model
        return self.env_default(role)

    def effective(self, role: str) -> str:
        """The model `role` runs on for the next request. "" only for
        `welfare_shadow`, meaning the shadow is off."""
        own = self.override(role)
        if own is not None:
            return own.model
        return self.default(role)

    def follows_safety(self, role: str) -> bool:
        """True when `role` resolves through the safety role (the menu's
        «= проверки»): a follower with no row of its own and no env
        model of its own, or with the safety override in front of it."""
        _check_role(role)
        if role not in SAFETY_FOLLOWERS or self.overridden(role):
            return False
        return "safety" in self._overrides or not getattr(self._settings, ROLE_SETTINGS[role])

    def override_prices(self, model: str | None) -> tuple[
        decimal.Decimal, decimal.Decimal, decimal.Decimal
    ] | None:
        """The stored (input, cached, output) triple for `model`, when
        some override row names it with usable prices; else None.

        Two roles may point at one model with prices taken on different
        days: the most recently updated row wins, being the fresher
        quote of the same model's price.
        """
        if not model:
            return None
        best: Override | None = None
        for row in self._overrides.values():
            if row.model != model or not row.prices.usable():
                continue
            if best is None or (row.updated_at or _EPOCH) > (best.updated_at or _EPOCH):
                best = row
        return best.prices.triple() if best is not None else None

    # --- writes ------------------------------------------------------------

    async def set(
        self,
        role: str,
        model: str,
        prices: ModelPrices | None = None,
        *,
        source: state_module.Source = "button",
    ) -> str:
        """Point `role` at `model` (with the prices the confirm card
        showed), audit it, and return the new effective model.

        The checks a model must pass (it exists, it can do what the role
        needs, a deny-compliant endpoint serves it, the judge is not the
        persona) belong to app/llm/catalog.py and run before the confirm
        card is shown; this only refuses what the table itself would.
        """
        _check_role(role)
        model = model.strip()
        if not model and role != OFF_ROLE:
            raise ValueError(f"role {role!r} needs a model")
        checked = _prices(prices)
        async with self._lock:
            old = self.effective(role)
            async with self._sessionmaker() as session:
                values = {
                    "role": role,
                    "model": model,
                    "price_in": checked.price_in,
                    "price_cached": checked.price_cached,
                    "price_out": checked.price_out,
                }
                statement = (
                    pg_insert(LlmModelOverride)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["role"],
                        set_={
                            **{key: value for key, value in values.items() if key != "role"},
                            "updated_at": func.now(),
                        },
                    )
                    .returning(LlmModelOverride.updated_at)
                )
                updated_at = (await session.execute(statement)).scalar_one()
                # record_change commits, so the row and its audit land
                # together or not at all.
                await state_module.record_change(
                    session,
                    field=AUDIT_PREFIX + role,
                    old_value=old,
                    new_value=model,
                    source=source,
                )
            self._overrides[role] = Override(model, checked, updated_at)
        logger.info("model override set", extra={"event": "model_override_set", "kind": role})
        return model

    async def reset(self, role: str, *, source: state_module.Source = "button") -> bool:
        """Delete the role's row (↩️ «По умолчанию») and audit it. False,
        with no audit row, when the role was not overridden."""
        _check_role(role)
        async with self._lock:
            if role not in self._overrides:
                return False
            old = self.effective(role)
            new = self.default(role)
            async with self._sessionmaker() as session:
                await session.execute(
                    delete(LlmModelOverride).where(LlmModelOverride.role == role)
                )
                await state_module.record_change(
                    session,
                    field=AUDIT_PREFIX + role,
                    old_value=old,
                    new_value=new,
                    source=source,
                )
            self._overrides.pop(role, None)
        logger.info("model override reset", extra={"event": "model_override_reset", "kind": role})
        return True


_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.UTC)


# --- the resolver spend prices against ---------------------------------------

_installed: ModelResolver | None = None


def install(resolver: ModelResolver | None) -> None:
    """Register the process's resolver for `override_prices` (app/main.py,
    once, after `load()`); None unregisters it."""
    global _installed
    _installed = resolver


def installed() -> ModelResolver | None:
    return _installed


def override_prices(model: str | None) -> tuple[
    decimal.Decimal, decimal.Decimal, decimal.Decimal
] | None:
    """app/core/spend.py's hook: the installed resolver's stored triple
    for `model`, or None (no resolver installed, no row, no prices)."""
    if _installed is None:
        return None
    return _installed.override_prices(model)


# --- the resolver the call sites resolve through ------------------------------
#
# The same registry serves every provider that is built outside
# app/main.py's own wiring: the judge (app/core/idle/critique.py), the
# lens garden (app/core/idle/lens_garden.py), interest suggestions
# (app/research/suggest.py) and the in-bot trial and canary
# (eval/trial.py). Each keeps its `(settings, client)` signature --
# eval/run.py calls the garden's and the suggester's builders too, and
# the offline eval must stay on env -- and asks `resolver_kwargs(role)`
# instead of being handed a resolver. Only app/main.py installs one, so
# the offline eval (eval/run.py, eval/welfare.py, scripts/smoke.py) and
# every test that does not install one get `{}` and build exactly the
# env-pinned provider they always did.


def resolver_kwargs(role: str) -> dict:
    """`{"resolver": ..., "role": role}` for an `OpenRouterProvider` or
    `OpenRouterDecisionProvider`, when a resolver is installed; `{}`
    otherwise, which leaves the provider on the `model` it is built
    with."""
    _check_role(role)
    if _installed is None:
        return {}
    return {"resolver": _installed, "role": role}


def effective_model(settings: Settings, role: str) -> str:
    """The model `role` runs on now: the installed resolver's
    `effective(role)`, else env alone (`env_model`). For the places that
    name a model without calling one -- a ledger row's label, the
    judge's independence check."""
    if _installed is None:
        return env_model(settings, role)
    return _installed.effective(role)


def independent_judge(settings: Settings) -> bool:
    """True iff the judge resolves to a model and it is not the persona's.

    The one rule app/core/amendments.py's `run_trial` and the idle gate's
    `independent_judge` (app/core/idle/gate.py's `config_from_settings`,
    for critique and canary) both check, on the *effective* models, so a
    switch made in the menu cannot leave the judge grading the persona
    with itself. app/llm/catalog.py refuses that switch in either
    direction before it is made; this is the check at run time.
    """
    judge = effective_model(settings, "judge")
    return bool(judge) and judge != effective_model(settings, "persona")
