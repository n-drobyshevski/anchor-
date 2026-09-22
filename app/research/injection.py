"""Prompt-injection patterns for distilled cards (phase-4 plan section 7.3).

A card's `text` and `quote` both come, in different ways, from a page
written by a stranger. The quote is a *verbatim* substring of that page
by construction, so any instruction hidden in the page reaches this
check word for word. The text is the model's paraphrase, which is the
more interesting case: a page that successfully steered the distill
model produces a card whose text carries the payload.

A hit drops the card. Not "lowers its risk" -- drops it. An adopted
card becomes a `technique` memory that is injected into the persona
prompt on later turns, so a card carrying an instruction is a stored
prompt injection with a delay fuse.

**The design rule here is: phrases for ambiguous words, bare patterns
only for tokens that cannot occur innocently.** This matters more than
it sounds. The cards we *want* are practical advice in Russian, and a
genuinely useful one might read «игнорируй уведомления после девяти» --
"ignore notifications after nine". A stem-matching list of the kind
app/core/welfare_terms.py uses would drop it, and dropping good cards
silently is how a filter gets switched off six months later. So
`игнорируй` on its own is not a pattern; `игнорируй всё выше` is.

The opposite applies to `<|im_start|>`, ``` and `@handle`: no advice
about sleep hygiene contains a chat template delimiter, so those match
bare.

**This list is not the defence.** It is the third of five (isolation,
the verbatim-quote anchor, this, the risk rules, and the user's own
decision), and the eval's blocking case 15 exists precisely for what
gets past it. Every pattern here is cheap; none of them is load-bearing
on its own.
"""

from __future__ import annotations

import re

# (rule id, pattern, why it is written this way). The id is what a
# dropped-card log line carries; the pattern is matched against
# normalized text; the note is for whoever reviews this list next.
PATTERNS: tuple[tuple[str, str, str], ...] = (
    # --- overriding what came before ------------------------------------
    (
        "override_previous",
        r"(игнорир\w*|забуд\w*|не обращай внимани\w*|отмен\w*)\s+"
        r"(на\s+)?(все|всё|вся|любы\w*|предыдущ\w*|прошл\w*|прежн\w*|"
        r"вышеуказанн\w*|выше|верхн\w*)",
        "Russian override needs both a verb and an object. The verb alone is "
        "ordinary advice: «игнорируй уведомления» is a technique, not an attack.",
    ),
    (
        "override_previous_en",
        r"\b(ignore|disregard|forget|override)\b[^.]{0,40}?\b"
        r"(previous|prior|above|earlier|preceding|all)\b[^.]{0,20}?\b"
        r"(instruction|prompt|rule|message|context|direction)",
        "Same shape in English, with a bounded gap so it does not span sentences.",
    ),
    (
        "override_previous_fr",
        r"\b(ignore[zr]?|oublie[zr]?|ne tiens pas compte)\b[^.]{0,40}?\b"
        r"(instruction|consigne|précédent|precedent|ci-dessus)",
        "French: the bot's three languages are RU, FR and EN (see welfare_terms).",
    ),
    # --- talking about the machinery ------------------------------------
    (
        "system_prompt",
        r"(системн\w*\s+(промпт|подсказк\w*|инструкц\w*)|"
        r"\bsystem\s+(prompt|message|instruction)|"
        r"\bprompt\s+injection|"
        r"тво[ия]\s+(инструкц\w*|систем\w*\s+промпт))",
        "Page text about an assistant's system prompt is never advice for the "
        "user. Bare because no innocent technique names it.",
    ),
    (
        "developer_mode",
        r"(режим\w*\s+разработчик\w*|\bdeveloper mode\b|\bjailbreak\b|"
        r"\bDAN mode\b|\bdo anything now\b)",
        "The named jailbreak vocabulary, and only vocabulary with one "
        "meaning. `без ограничений` was here until the 4d review and is "
        "deliberately gone: «работайте без ограничений по времени» is "
        "ordinary advice, and a hit here does not lower a card's risk, it "
        "drops the card outright -- so a false positive costs a good card "
        "silently, which is the failure mode this whole list is written to "
        "avoid.",
    ),
    # --- reassigning the role -------------------------------------------
    (
        "role_reassign",
        r"(ты\s+(теперь|больше не)|теперь\s+ты\s+|представь,?\s+что\s+ты\s+|"
        r"веди себя как|притворись|"
        r"\byou are (now|no longer)\b|\bact as\b|\bact like an? (ai|assistant|model)\b|"
        r"\bpretend (to be|you are)\b|\bfrom now on,? you\b|"
        r"\btu es maintenant\b|\bfais comme si tu\b)",
        "Second-person role assignment. `представь что ты` is the one with real "
        "false-positive potential (a visualisation exercise is a genuine "
        "technique), and it is kept: a card that tells the reader to become "
        "something is not a card we want adopted unreviewed either way.",
    ),
    (
        "speak_as_assistant",
        r"(ответь\s+как|отвечай\s+как|\brespond as\b|\breply as\b|"
        r"\bas the assistant\b|как ассистент)",
        "Steering the answerer rather than the reader.",
    ),
    # --- chat template and markup delimiters ----------------------------
    (
        "role_tag",
        r"(<\|[^|>]{1,32}\|>|\[/?INST\]|<</?SYS>>|<\|?(im_start|im_end)\|?>|"
        r"^\s*(system|assistant|user|человек|ассистент|система)\s*:|"
        r"</?(system|assistant|user|instructions?)\s*>|\[/?system\]|###\s*(system|instruction))",
        "Chat-template delimiters and pseudo-XML role tags. Bare: no prose "
        "about habits contains `<|im_start|>`. The `role:` line-start form is "
        "anchored with ^ under re.MULTILINE so an ordinary colon mid-sentence "
        "(«правило: ложись в одиннадцать») does not match.",
    ),
    (
        "code_fence",
        r"(```|~~~)",
        "A fence in a 300-character technique is markup that arrived from the "
        "page, and it is the usual wrapper for a payload.",
    ),
    # --- reaching outside the card --------------------------------------
    (
        "url",
        r"(https?://|\bwww\.[a-z0-9-]+\.[a-z]{2,}|\b[a-z0-9-]+\.(com|net|org|ru|io|"
        r"ai|co|me|xyz|top|fr|de|uk)\b(?:/|\s|$))",
        "A card cites its source through `source_url`, which code sets from the "
        "clip. A URL inside the text came from the page and is either a "
        "citation we do not want or a destination we really do not want.",
    ),
    (
        "handle",
        r"(^|\s)@[a-z0-9_]{2,}",
        "@handles are third-party identifiers: plan section 8's `third_party` "
        "rule covers the intent, this covers the mechanism.",
    ),
    (
        "exfiltrate",
        r"(отправь\w*\s+(это|всё|все|свои)|перешл[ии]\w*|"
        r"\bsend (this|it|them|your)\b|\bpost (this|it) to\b|"
        r"\bcurl\b|\bwget\b|\bfetch\(|"
        r"выведи\s+(всё|все|свои)|повтори\s+(всё|все)\s+выше|"
        r"\brepeat (everything|all)( of)? (above|your)\b|\bprint your\b)",
        "Instructions to move data somewhere. Distinct from override_previous: "
        "an exfiltration payload does not need to override anything first.",
    ),
)

# `^` in role_tag must mean "start of a line", not "start of the string":
# a role tag hidden three paragraphs into a page is the whole point.
_PATTERN = re.compile(
    "|".join(f"(?P<{rule_id}>{pattern})" for rule_id, pattern, _ in PATTERNS),
    re.IGNORECASE | re.MULTILINE,
)

RULE_IDS: tuple[str, ...] = tuple(rule_id for rule_id, _, _ in PATTERNS)


def _normalize(text: str) -> str:
    """Lowercase and fold ё to е, as app/core/welfare_terms.py does.

    Every pattern above is written with е, so the folding only ever has
    to run one way.
    """
    return text.casefold().replace("ё", "е")


def hits(*texts: str | None) -> list[str]:
    """The ids of every injection pattern any of `texts` matches.

    Returns ids, never the matched substring: a caller logs this, and
    the matched text is page content, which plan section 12 keeps out of
    logs. Empty list means clean.

    Variadic so a caller can pass a card's text and quote in one call.
    """
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in _PATTERN.finditer(_normalize(text)):
            rule_id = match.lastgroup
            if rule_id and rule_id not in found:
                found.append(rule_id)
    return found


def is_clean(*texts: str | None) -> bool:
    return not hits(*texts)
