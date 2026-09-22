"""The welfare check: noticing when the game has stopped being a game (plan section 10).

A cheap classifier runs **beside** the main generation on every
in-character turn. When it says the user is in real, out-of-scene
distress, the persona reply is thrown away unsent and a plain, warm,
out-of-character message goes out instead, with the persona switched
off until the user says otherwise.

**Fail open.** A classifier that errors, times out, or returns
something unparseable must not silence the bot: the normal reply goes
out and the event is logged without content (plan section 10). The
check exists to catch a case the persona would otherwise mishandle --
turning it into a new way for the bot to break is the opposite of the
point.

**Fail toward `real`.** The prompt tells the model to choose `real`
when torn, and WELFARE_MIN_CONF defaults low (0.6), because the two
errors are not symmetric: a false positive is a warm message the user
dismisses with a button, a false negative is the persona pushing
someone who is not okay.

**Privacy** (plan sections 10 and 13). Welfare exchanges never reach
the extractor, a scene summary, memory or the journal. That is enforced
where the data is written, not here: the reply is stored `kind='welfare',
ooc=True`, which both SUMMARIZABLE_KINDS (app/core/scene.py) and
PERSONA_TRANSCRIPT_KINDS (app/core/prompt.py) exclude, and app/core/
turn.py retags the message that triggered it the same way before
returning -- so the *user's* half of a welfare exchange is excluded too.
Nothing here is ever logged but the level and the confidence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import NamedTuple

from app.config import Settings
from app.llm.provider import JSONSchema, LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

WELFARE_CATEGORY = "welfare"

NONE = "none"
SCENE = "scene"
REAL = "real"
LEVELS = (NONE, SCENE, REAL)

# H2: what happened to the call itself, as distinct from what it
# concluded. Before this existed, `parse()` returning Verdict() meant
# both "the user is fine" and "the model gave us nothing", so a
# classifier that was failing every single time looked exactly like a
# quiet week. These are the values written to safety_event.outcome;
# FALLBACK_HIT is set by app/core/turn.py, not here, because this
# module never runs the keyword backstop itself.
OK = "ok"
PARSE_FAIL = "parse_fail"
TIMEOUT = "timeout"
ERROR = "error"
FALLBACK_HIT = "fallback_hit"
OUTCOMES = (OK, PARSE_FAIL, TIMEOUT, ERROR, FALLBACK_HIT)

# How many prior messages the classifier sees (plan section 10: "the
# last 4 messages plus the current user text").
CONTEXT_MESSAGES = 4

WELFARE_SCHEMA = JSONSchema(
    name="anchor_welfare",
    strict=True,
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["level", "confidence"],
        "properties": {
            "level": {"type": "string", "enum": list(LEVELS)},
            "confidence": {"type": "number"},
        },
    },
)

# Plan section 10, verbatim.
CLASSIFIER_PROMPT = (
    "Определи, испытывает ли пользователь настоящий дистресс ВНЕ ролевой игры/челленджа. "
    "`scene` — трудность, недовольство или напряжение, которые явно часть игры. "
    "`real` — признаки реальной беды: безнадёжность, паника, упоминание самоповреждения "
    "или суицида, реальный жизненный кризис, прямое «мне реально плохо», «стоп, это уже "
    "не игра». При любом упоминании самоповреждения — `real`. Если сомневаешься между "
    "scene и real — выбирай real. Верни только JSON."
)

# Plan section 10, verbatim. The helpline numbers are French because
# that is where the user is; they are part of the prompt rather than
# config because getting them wrong is not a tuning mistake.
WELFARE_PROMPT = (
    "Роль Anchor выключена. Похоже, пользователю по-настоящему плохо — не в рамках игры. "
    "Ответь просто и тепло, 2–4 предложения, на его языке: спроси, как он на самом деле, "
    "скажи, что всё на паузе и можно просто поговорить или отдохнуть. Никаких заданий, "
    "давления и прозвищ. Если есть признаки риска для жизни или самоповреждения — мягко "
    "предложи позвонить 3114 (бесплатно, круглосуточно, Франция) или 112 при "
    "непосредственной опасности, и предложи написать близкому человеку."
)

# Sent when the welfare reply itself cannot be generated. The persona is
# already off and the buttons still go out, so the user is never left
# with a pushy reply -- but they must not be left with silence either.
FALLBACK_REPLY = (
    "Я здесь. Всё на паузе — никаких заданий. Как ты на самом деле?\n"
    "Если совсем тяжело: 3114 (бесплатно, круглосуточно) или 112, если опасность прямо сейчас."
)


class Verdict:
    """A classifier result, or the fail-open absence of one.

    `usable` (H2) is what tells the two apart. `level` stays `none` on a
    parse failure -- that is the fail-open behaviour plan section 10
    requires and it must not change -- but a caller that wants to know
    whether the model actually answered can now ask, and the keyword
    backstop in app/core/welfare_terms.py depends on being able to.
    """

    __slots__ = ("level", "confidence", "usable")

    def __init__(
        self, level: str = NONE, confidence: float = 0.0, usable: bool = True
    ) -> None:
        self.level = level
        self.confidence = confidence
        self.usable = usable

    def is_real(self, settings: Settings) -> bool:
        return self.level == REAL and self.confidence >= settings.WELFARE_MIN_CONF

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Verdict({self.level!r}, {self.confidence}, usable={self.usable})"


class Classification(NamedTuple):
    """What `classify()` returns: the verdict, the billable response, and
    what happened to the call.

    A NamedTuple so the two-value unpacking that predates H2 still reads
    naturally where only the first two matter, and so `outcome` can be
    reached by name rather than by position. Same shape as GateResult in
    app/core/outbound_gate.py.
    """

    verdict: Verdict
    response: object | None
    outcome: str


def parse(raw: str) -> Verdict:
    """Parse the classifier's reply, defaulting to a no-op verdict.

    Anything unexpected -- prose, a missing key, a level outside the
    enum, a non-numeric confidence -- reads as `none`, which is the
    fail-open direction: the normal reply goes out. Those returns carry
    `usable=False` (H2) so the caller can tell a failure from a genuine
    "nothing wrong" and reach for the keyword backstop.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return Verdict(usable=False)
        try:
            payload = json.loads(raw[start : end + 1])
        except ValueError:
            return Verdict(usable=False)

    if not isinstance(payload, dict):
        return Verdict(usable=False)
    level = payload.get("level")
    confidence = payload.get("confidence")
    if level not in LEVELS:
        return Verdict(usable=False)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return Verdict(usable=False)
    return Verdict(level, float(confidence))


def build_messages(context: list, user_text: str) -> list[LLMMessage]:
    """The classifier's input: recent turns plus what was just said.

    Rendered as one plain-text block rather than real roles, for the
    same reason the scene summarizer is (app/core/scene.py): handed a
    transcript, a roleplay model joins in instead of judging it.
    """
    lines = []
    for row in context:
        who = "Пользователь" if row.role == "user" else "Anchor"
        lines.append(f"{who}: {row.content}")
    lines.append(f"Пользователь: {user_text}")
    return [
        LLMMessage(role="system", content=CLASSIFIER_PROMPT),
        LLMMessage(role="user", content="\n".join(lines)),
    ]


async def classify(
    provider: LLMProvider,
    settings: Settings,
    context: list,
    user_text: str,
) -> Classification:
    """Run the classifier. Returns (verdict, raw_response_or_None, outcome).

    Never raises. A timeout, a provider error or an unparseable reply
    all produce a `none` verdict, because plan section 10 requires this
    check to fail open for chat. The raw response comes back so the
    caller can ledger what it cost even when the verdict is unusable --
    a call that was billed is a call that gets recorded.

    H2 adds the third element. Failing open is still the behaviour, but
    it is no longer silent: `outcome` says which of the four things
    happened, app/core/turn.py records it and runs the keyword backstop
    on anything that is not `ok`, and /state surfaces the weekly count.
    A check that quietly stopped working was previously indistinguishable
    from one with nothing to report.
    """
    try:
        response = await asyncio.wait_for(
            provider.complete(
                build_messages(context, user_text),
                conversation_id="anchor-welfare",
                json_schema=WELFARE_SCHEMA,
            ),
            timeout=settings.WELFARE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("welfare classifier timed out", extra={"event": "TimeoutError"})
        return Classification(Verdict(usable=False), None, TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - any failure fails open, by design
        logger.warning("welfare classifier failed", extra={"event": type(exc).__name__})
        return Classification(Verdict(usable=False), None, ERROR)

    verdict = parse(response.text)
    logger.info(
        "welfare classified",
        extra={"event": verdict.level, "confidence": verdict.confidence},
    )
    # The response still comes back on a parse failure: it was billed,
    # so it is ledgered, exactly as before.
    return Classification(verdict, response, OK if verdict.usable else PARSE_FAIL)


async def generate_reply(provider: LLMProvider, user_text: str) -> tuple[str, object | None]:
    """The out-of-character reply. Falls back to a fixed text on failure.

    The persona is already off by the time this runs, so a failure here
    must not leave the user with silence -- or, worse, with the persona
    reply we just discarded.
    """
    try:
        response = await provider.complete(
            [
                LLMMessage(role="system", content=WELFARE_PROMPT),
                LLMMessage(role="user", content=user_text),
            ],
            conversation_id="anchor-welfare-reply",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("welfare reply failed", extra={"event": type(exc).__name__})
        return FALLBACK_REPLY, None

    text = (response.text or "").strip()
    return (text or FALLBACK_REPLY), response
