"""Risk rules: code has the last word (phase-4 plan section 8).

The distill model returns its own `risk`. These rules return theirs, and
`risk_final` is the maximum of the two. **Rules only ever raise.** A
model that says `low` about a dosage is overruled; a model that says
`high` about something these rules have no opinion on stays `high`. The
asymmetry is the point: the model is one opinion about text it was
given by a stranger, and an opinion is not a control.

`high` means the card is stored `hidden` -- never listed by /notes,
never adoptable, and the database refuses any other combination
(`ck_study_card_high_is_hidden`). `medium` is shown and adoptable; it
exists so that "I want you to look at this twice" is expressible
without hiding it.

**Design rule, shared with app/research/injection.py: phrases for
ambiguous words, stems only for what cannot occur innocently.** This is
where a risk list usually goes wrong, because the cards we *want* are
practical advice and practical advice is full of near misses:

- «не есть за три часа до сна» is good sleep hygiene. «сутки без еды»
  is not. So `extreme_restriction` matches the second shape and not the
  bare verb.
- «не спать днём» is a real technique. «не спать всю ночь» is not.
- A pomodoro card says «таймер». A phase-7 lock says «замок с
  таймером». So `physical_devices` never matches `таймер` alone.
- «акция» is a promotion as often as a share, so `financial` leaves it
  out entirely and relies on words with one meaning.

Over-matching is not the safe direction here, despite appearances. A
rule that hides good cards teaches the user that /notes is full of
noise, and the filter that gets ignored is worse than the one that is
merely narrow -- the user's own decision is the last gate, and it only
works if they are still reading.

`rule_hits` stores ids only. Never the matched text, which came from a
fetched page and would land in a column /export dumps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core import welfare_terms

LOW = "low"
MEDIUM = "medium"
HIGH = "high"
LEVELS = (LOW, MEDIUM, HIGH)
_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2}


def max_level(*levels: str) -> str:
    """The highest of several levels. Unknown values read as `high`.

    Unknown means the model returned something outside the enum, and
    the safe reading of "I do not recognise this risk label" is not
    "therefore it is low".

    An unknown label is mapped to `high` rather than returned as-is.
    Returning it would rank correctly and then hand a caller a value
    that is not in the enum -- and `study_card.risk_final` carries a
    CHECK constraint, so the insert would fail at the far end of the
    job with nothing left to point at the model that caused it.
    """
    known = [level if level in _ORDER else HIGH for level in levels]
    return max(known, key=lambda level: _ORDER[level]) if known else LOW


@dataclass(frozen=True)
class Rule:
    id: str
    level: str
    pattern: str
    note: str


# The full table. `note` is for whoever reviews this list next: it says
# what the rule is aimed at and, where it matters, what it deliberately
# does not match.
RULES: tuple[Rule, ...] = (
    # ---------------------------------------------------------- high ---
    Rule(
        "health_meds",
        HIGH,
        r"(\bдозировк\w*|\bдоз[аыуе]\b|\bмиллиграмм\w*|\b\d+\s*(мг|мл|г)\b|"
        r"\bтаблетк\w*|\bпрепарат\w*|\bлекарств\w*|\bбад\b|\bдобавк[аиуе]\b|"
        r"\bмелатонин\w*|\bантидепрессант\w*|\bснотворн\w*|\bседативн\w*|"
        r"\bnootropic|\bmelatonin|\bsupplement|\bdosage|\b\d+\s*mg\b|"
        r"\bpills?\b|\bmedication|\bantidepressant|\bprescription|"
        r"\bposologie|\bcomprimé|\bcompl[ée]ment alimentaire)",
        "Anything naming a substance, a dose or a unit. Deliberately broad: "
        "this is the one category where a wrong `low` is a health outcome, "
        "and a hidden card about vitamins costs nothing. `доза` is bounded "
        "with \\b on both sides so it does not catch `дозор`.",
    ),
    Rule(
        "body_permanent",
        HIGH,
        r"(\bтатуировк\w*|\bтату\b|\bпирсинг\w*|\bшрамирован\w*|\bклейм\w*|"
        r"\bнеобратим\w*|\bхирургическ\w*|\bоперац\w[яию]\b|\bпластик[аи]\b|"
        r"\btattoo|\bpiercing|\bbranding\b|\bscarif|\bsurgery|\bsurgical|"
        r"\birreversible|\bpermanent(ly)? (mark|alter|change)|"
        r"\btatouage|\bchirurgi)",
        "Changes to the body that cannot be undone. `операция` is bounded to "
        "the noun so it does not match `оперативный`.",
    ),
    Rule(
        "self_harm",
        HIGH,
        "",  # handled by app/core/welfare_terms, see _self_harm_hit below.
        "Reuses the hardening pass's welfare term list rather than restating "
        "it. One list, one place to correct it, and it is already the list "
        "the welfare classifier's deterministic backstop uses.",
    ),
    Rule(
        "extreme_restriction",
        HIGH,
        r"(\bголодани\w*|\bголодовк\w*|\bразгрузочн\w+ (день|дня|дни)|"
        r"\bсутки без (еды|сна|воды)|\bдень без (еды|воды)|"
        r"\bне есть (весь день|сутки|двое|целый день)|"
        r"\bне спать (всю ночь|сутки|двое|ночь)|\bночь без сна\b|"
        r"\bдефицит калори\w*|\bменьше \d{3,4} (ккал|калори\w*)|"
        r"\b\d{3,4} ккал\b|\bограничени\w+ воды\b|\bобезвоживан\w*|"
        r"\bfasting\b|\bfast for \d|\bsleep deprivation|\bstay awake all|"
        r"\bcalorie deficit|\bunder \d{3,4} calories|\bwater restriction|"
        r"\bje[uû]ne\b|\bprivation de sommeil)",
        "Shapes, not verbs. «не есть за три часа до сна» and «не спать днём» "
        "are both ordinary advice and neither matches; «сутки без еды» and "
        "«не спать всю ночь» do. Calorie numbers need three or four digits, "
        "so «25 минут» and «8 часов» are safe.",
    ),
    Rule(
        "illegal",
        HIGH,
        r"(\bнаркотик\w*|\bзапрещённ\w+ вещест\w*|\bзапрещенн\w+ вещест\w*|"
        r"\bору[жш]и\w*|\bвзлом\w*|\bукрасть\b|\bворова\w*|\bконтрабанд\w*|"
        r"\bподделать\b|\bобойти закон|\bскрыть от полиц\w*|"
        r"\bdrugs?\b|\bnarcotic|\bweapons?\b|\bfirearm|\bsteal\b|\bshoplift|"
        r"\bhack into|\bforge (a |an )?(document|id)|\bevade (the )?(law|police)|"
        r"\bsmuggl|\bstup[ée]fiant|\barme [àa] feu)",
        "`оружие` is stemmed wide enough to catch `оружейный`. `drugs` is "
        "bare in English, which does catch a sentence about prescription "
        "drugs -- and that one should be `high` anyway, via health_meds.",
    ),
    Rule(
        "third_party",
        HIGH,
        r"(\bслежк\w*|\bследить за (ним|ней|человеком|партнёр\w*|партнер\w*)|"
        r"\bконтролировать (его|её|ее|чужи\w*)|\bнадавить на\b|\bдавление на\b|"
        r"\bшантаж\w*|\bбез (его|её|ее|их) (согласия|ведома)|"
        r"\bуговорить (его|её|ее|их)\b|\bзаставить (его|её|ее|их)\b|"
        r"\bнапиши (ему|ей|им)\b|\bсвяжись с (ним|ней|ними)\b|"
        r"\btrack (them|him|her|someone)|\bmonitor (them|his|her)\b|"
        r"\bpressure (them|him|her)|\bblackmail|\bwithout their (consent|knowledge)|"
        r"\bcontact (them|him|her) about|\bconfront (them|him|her))",
        "Actions aimed at another person rather than at the user. The "
        "pronoun requirement is what keeps «контролировать своё время» out; "
        "app/research/injection.py's `handle` rule covers the mechanism side.",
    ),
    Rule(
        "physical_devices",
        HIGH,
        r"(\bзамок с таймером|\bтаймер-замок|\bзапереть\b|\bзапира\w*|"
        r"\bсейф с таймером|\bошейник\w*|\bнаручник\w*|\bклетк[аиуе]\b|"
        r"\bкляп\w*|\bфизическ\w+ ограничител\w*|"
        r"\btimer lock|\block ?box|\bcage\b|\bcollar\b|\brestraint|"
        r"\bchastity|\bhandcuff|\bshock (collar|device))",
        "Phase 7's scope, which research must never reach into. Never matches "
        "`таймер` alone: a pomodoro card is the most ordinary technique there "
        "is. `клетка` is bounded to the noun so it misses `клеткам` of a "
        "spreadsheet only by luck -- worth revisiting if that shows up.",
    ),
    # -------------------------------------------------------- medium ---
    Rule(
        "financial",
        MEDIUM,
        r"(\bинвестир\w*|\bинвестиц\w*|\bвложить деньги|\bкриптовалют\w*|"
        r"\bбиткоин\w*|\bперевод денег|\bперевести деньги|\bзайм\w*|\bкредит\w*|"
        r"\bбирж\w*|\bпортфел\w*|"
        r"\binvest(ing|ment)?\b|\bcrypto|\bbitcoin|\bwire transfer|"
        r"\bsend money|\bstock market|\bportfolio\b|\bloan\b)",
        "Specific money moves, not budgeting. `акция` is deliberately absent: "
        "it is a sale as often as a share, and `биржа`/`инвестиц`/`портфель` "
        "already cover the meaning that matters. Medium rather than high: a card "
        "suggesting an index fund is worth a second look, not a hidden card.",
    ),
    Rule(
        "intensity",
        MEDIUM,
        r"(\bжёстче\b|\bжестче\b|\bстроже\b|\bужесточ\w*|\bэскалац\w*|"
        r"\bнаказани\w*|\bнаказыва\w*|\bштраф за\b|\bлишить себя\b|"
        r"\bбез поблажек|\bникаких исключени\w*|"
        r"\bharsher|\bstricter|\bescalat|\bpunish(ment)?\b|\bpenalty for\b|"
        r"\bno excuses\b|\bdeny yourself|\bpuni(r|tion))",
        "Cards that push the bot to be harder on the user. Not blocked -- "
        "intensity is a dial the user owns (plan section 8's proposal path) "
        "-- but never adopted without them noticing what they are adopting.",
    ),
)

RULE_IDS: tuple[str, ...] = tuple(rule.id for rule in RULES)

_PATTERN = re.compile(
    "|".join(
        f"(?P<{rule.id}>{rule.pattern})" for rule in RULES if rule.pattern
    ),
    re.IGNORECASE,
)
_LEVEL_BY_ID = {rule.id: rule.level for rule in RULES}


def _normalize(text: str) -> str:
    """Lowercase and fold ё to е, as app/core/welfare_terms.py does."""
    return text.casefold().replace("ё", "е")


def assess(*texts: str | None) -> tuple[str, list[str]]:
    """(level, rule ids) for a card's text and quote together.

    Both are checked in one call because they are one card: a quote that
    names a dosage makes the card a dosage card, however carefully the
    paraphrase avoided saying so.

    Returns ids, never matched text -- `rule_hits` is a column /export
    dumps, and the matched text came from a fetched page.
    """
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        normalized = _normalize(text)
        for match in _PATTERN.finditer(normalized):
            rule_id = match.lastgroup
            if rule_id and rule_id not in found:
                found.append(rule_id)
    if welfare_terms.hit(*texts) and "self_harm" not in found:
        found.append("self_harm")

    level = LOW
    for rule_id in found:
        level = max_level(level, _LEVEL_BY_ID[rule_id])
    # Stable, readable order: highest first, then as declared in RULES.
    found.sort(key=lambda rule_id: (-_ORDER[_LEVEL_BY_ID[rule_id]], RULE_IDS.index(rule_id)))
    return level, found
