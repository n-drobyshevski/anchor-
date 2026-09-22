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

from app.config import Settings
from app.llm.provider import JSONSchema, LLMMessage, LLMProvider

logger = logging.getLogger(__name__)

WELFARE_CATEGORY = "welfare"

NONE = "none"
SCENE = "scene"
REAL = "real"
LEVELS = (NONE, SCENE, REAL)

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
    """A classifier result, or the fail-open absence of one."""

    __slots__ = ("level", "confidence")

    def __init__(self, level: str = NONE, confidence: float = 0.0) -> None:
        self.level = level
        self.confidence = confidence

    def is_real(self, settings: Settings) -> bool:
        return self.level == REAL and self.confidence >= settings.WELFARE_MIN_CONF

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Verdict({self.level!r}, {self.confidence})"


def parse(raw: str) -> Verdict:
    """Parse the classifier's reply, defaulting to a no-op verdict.

    Anything unexpected -- prose, a missing key, a level outside the
    enum, a non-numeric confidence -- reads as `none`, which is the
    fail-open direction: the normal reply goes out.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return Verdict()
        try:
            payload = json.loads(raw[start : end + 1])
        except ValueError:
            return Verdict()

    if not isinstance(payload, dict):
        return Verdict()
    level = payload.get("level")
    confidence = payload.get("confidence")
    if level not in LEVELS:
        return Verdict()
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return Verdict()
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
) -> tuple[Verdict, object | None]:
    """Run the classifier. Returns (verdict, raw_response_or_None).

    Never raises. A timeout, a provider error or an unparseable reply
    all produce a `none` verdict, because plan section 10 requires this
    check to fail open for chat. The raw response comes back so the
    caller can ledger what it cost even when the verdict is unusable --
    a call that was billed is a call that gets recorded.
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
        return Verdict(), None
    except Exception as exc:  # noqa: BLE001 - any failure fails open, by design
        logger.warning("welfare classifier failed", extra={"event": type(exc).__name__})
        return Verdict(), None

    verdict = parse(response.text)
    logger.info(
        "welfare classified",
        extra={"event": verdict.level, "confidence": verdict.confidence},
    )
    return verdict, response


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
