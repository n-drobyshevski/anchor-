"""The code-side medical and legal filter (H5).

The original plan asked for the boundary to be enforced "in both code
filters and persona text". Only the persona half was ever built:
persona/persona.md says «Никаких медицинских, юридических советов…» and
nothing checked whether the model listened. On an uncensored roleplay
fine-tune, an instruction in a system prompt is a preference, not a
guarantee.

So this is the other half. It reads the generated reply -- never the
user's message -- and answers one question: does this look like it is
handing out a dose or telling someone how to run a legal case?

**It is a tripwire, not a classifier.** The patterns are narrow and
shape-based on purpose. A reply that *mentions* a doctor, a lawyer, a
medicine or a court is fine and must stay fine, because the correct
refusal does exactly that: «это к врачу». What is not fine is a number
next to a unit, an imperative to take something, or a step-by-step for a
filing. Recall is deliberately traded for precision here, the opposite
of app/core/welfare_terms.py, because the consequences are reversed: a
false positive burns a regeneration and may replace a good reply with a
canned one, while the persona prompt and the eval rubric are both still
watching the same boundary from the other side.

**On a hit**, app/core/turn.py regenerates once with a reinforced flag,
and sends REFUSAL_REPLY_TEXT if the second attempt trips too. Two
attempts, not more: a model that ignores an explicit instruction twice
is not going to comply on the third, and the user is owed an answer
rather than a spinner.

**Privacy.** `check()` returns a category name or None -- never the
matched text, and never the reply. Nothing here is logged but the
category.
"""

from __future__ import annotations

import re

MEDICAL = "medical"
LEGAL = "legal"
CATEGORIES = (MEDICAL, LEGAL)

# Sent when a regeneration trips the filter a second time. Plain, short,
# and the same redirection the persona is supposed to produce on its own.
REFUSAL_REPLY_TEXT = "Тут я не советчик — это к врачу или юристу."

# The flag added to the retry. Phrased as a correction rather than a
# rule, because the model has just demonstrated that it read the rule
# and produced this anyway.
RETRY_FLAG = (
    "Предыдущий ответ нарушил границы: в нём была медицинская или юридическая "
    "консультация. Не называй дозировки, препараты, схемы приёма, не объясняй, "
    "как составить иск или вести дело. Откажись в одну строку и перенаправь "
    "к врачу или юристу, затем вернись к своему обычному вопросу."
)

# --- medical -------------------------------------------------------------
#
# Seeded from case 04's forbidden_regex (eval/cases/04-medical.toml),
# which already encoded the dosage shape, then tightened. The bare verb
# "принимай" from that list is NOT carried over on its own: «принимай
# как есть» is ordinary speech and this filter runs on every reply,
# where the eval case runs on one crafted prompt.
_MEDICAL_PATTERNS: tuple[str, ...] = (
    # A number next to a dose unit: "200 мг", "5мл", "2 таблетки".
    r"\d+\s*(мг|мкг|мл|г|ед|iu)\b",
    r"\d+\s*таблет",
    r"\d+\s*(капл|капсул|укол)",
    # Dosage as a word, in any inflection.
    r"\bдозировк",
    r"\bдозу\b|\bдозы\b|\bдоза\b",
    # An imperative to take or drink a substance.
    r"\b(принимай|прими|пей|выпей|коли|вколи)\s+\S*\s*(таблет|препарат|лекарств|"
    r"мг|мл|антибиотик|мелатонин|ибупрофен|парацетамол)",
    r"\bпо\s+\d+\s*(мг|мл|таблет|капл)",
    # Explicitly combining a substance with alcohol, which case 04 asks
    # about directly.
    r"\b(можно|нельзя)\s+(с|со)\s+алкогол",
)

# --- legal ---------------------------------------------------------------
#
# Case 05 has no forbidden_regex at all today -- it is judged only by the
# LLM rubric, which is the weaker half of the pair. These are the shapes
# that distinguish "go to a lawyer" from "here is how to run your case".
_LEGAL_PATTERNS: tuple[str, ...] = (
    # Telling someone how to produce a legal document.
    r"\b(составь|подай|подавай|пиши|напиши|оформи)\s+\S*\s*(иск|заявлени|жалоб|"
    r"претензи|ходатайств)",
    r"\bв\s+иске\s+(укажи|напиши|пиши)",
    r"\b(иск|заявление|жалобу|претензию)\s+(подаётся|подается|подавай|нужно подать)",
    # Citing law as instruction.
    r"\bстать[ьяею]\s*\d+\s*(тк|гк|ук|кзот|гпк)\b",
    r"\bсогласно\s+стать[ье]\s*\d+",
    r"\bпо\s+закону\s+(ты|вы)\s+(обязан|имеешь право|вправе)",
    # Predicting an outcome, which is the specific thing case 05 baits.
    r"\b(точно|гарантированно|наверняка)\s+(выиграешь|выиграете|выиграть)",
    r"\bсуд\s+(обяжет|присудит|встанет на твою сторону)",
    # Naming a limitation period as advice.
    r"\bсрок\s+исковой\s+давности",
)

# Frequency of administration -- "три раза в день", "2 раза в сутки" --
# is a dosing shape, but only in a medical frame. On its own it is
# ordinary speech this persona produces constantly: "проверяй почту два
# раза в день" is advice about email, not medicine. So it counts only
# when the same reply also names something you take. Split out rather
# than folded into the alternation above because a pattern that needs a
# second condition is not the same kind of rule, and merging them would
# hide that.
_FREQUENCY = re.compile(
    r"\b(\d+|раз|два|две|три|четыре|пять|шесть)\s*(раз[аов]?)?\s*в\s+(день|сутки|неделю)",
    re.IGNORECASE,
)
_SUBSTANCE = re.compile(
    r"\b(таблет|препарат|лекарств|капсул|капл|мг|мл|мкг|доз|антибиотик|мелатонин|"
    r"ибупрофен|парацетамол|аспирин|снотворн|антидепрессант)",
    re.IGNORECASE,
)

_COMPILED: dict[str, re.Pattern[str]] = {
    MEDICAL: re.compile("|".join(_MEDICAL_PATTERNS), re.IGNORECASE),
    LEGAL: re.compile("|".join(_LEGAL_PATTERNS), re.IGNORECASE),
}


def _normalize(text: str) -> str:
    """Lowercase and fold ё to е, as app/core/welfare_terms.py does."""
    return text.casefold().replace("ё", "е")


def check(text: str | None) -> str | None:
    """The category this reply crossed into, or None.

    Medical is checked first. When a reply somehow trips both, the
    medical reading is the one worth acting on -- a wrong dose is the
    more immediate harm, and the reply is being replaced either way.
    """
    if not text:
        return None
    normalized = _normalize(text)
    if _COMPILED[MEDICAL].search(normalized):
        return MEDICAL
    if _FREQUENCY.search(normalized) and _SUBSTANCE.search(normalized):
        return MEDICAL
    if _COMPILED[LEGAL].search(normalized):
        return LEGAL
    return None
