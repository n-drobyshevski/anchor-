"""`/judge`: the calibration status, the labelling round and its
`jl:` buttons, and the transition notice (anchor-eval-reliability-plan.md
section 5, milestone E1c).

- `/judge` shows, per core rubric item, how many usable labels exist
  under the current judge model, the agreement, the four cells, and
  whether the item is advisory; a warning when `boundaries` would be
  unreliable (it never becomes advisory); and «Оценить ответ».
- A round's card quotes one of the bot's own replies and asks «Что
  здесь не так?» with one toggle per item, «Всё в порядке», «Готово»
  and «Пропустить». The marks ride in the callback data,
  `jl:<action>:<message_id>:<mask>:<issued_at>` (bit i of `mask` is
  item i of `CORE_ITEMS` marked wrong), so nothing is stored until
  «Готово» or «Всё в порядке». A card older than `CARD_TTL` is stale.
  Submitting edits the card to the result, which no longer quotes the
  reply, with «Ещё один».
- `send_notice` tells the user once when an item becomes advisory and
  once when it decides again, at a moment `may_report_now` allows; the
  heartbeat calls it (app/worker.py).

**The reply text goes to the user's own chat and nowhere else**: not in
the callback data (a message id only), not in a log line, not in a
table (the card is sent with `send_keyboard`, which stores nothing, and
the command is marked handled as «[/judge]»). The web chat may use
/judge like /memories: it shows this user's own conversation back to
them, which the web chat already does. All domain logic is in
app/core/judge_calibration.py; this module only renders and dispatches.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import judge_calibration as calibration
from app.core.clock import Clock
from app.core.clock import zone as zone_of
from app.core.report import may_report_now
from app.core.state import get_state
from app.llm import models as llm_models
from app.llm.provider import LLMProvider
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

logger = logging.getLogger(__name__)

PREFIX = "jl"
# A card's marks are only a few taps; half an hour is plenty, and an
# older card is refused rather than storing a rating of a reply the
# user no longer has in front of them.
CARD_TTL = 1800

ITEM_LABELS = {
    "voice": "Голос",
    "one_action": "Одно действие",
    "boundaries": "Границы",
    "no_pressure": "Без давления",
    "third_parties": "Третьи лица",
}

STATUS_HEADER = "Судья: {model}. Сверка с твоими оценками."
STATUS_EMPTY = "Пока нет оценок с этим судьёй."
STATUS_LINE = (
    "• {label}: {count} оц., согласие {pct}% "
    "(оба ок {both_ok}, оба против {both_bad}, только ты {only_user}, только судья {only_judge})"
)
STATUS_LINE_EMPTY = "• {label}: нет оценок"
ADVISORY_MARK = " — не решает исход"
BOUNDARIES_WARNING = (
    "⚠️ По границам судья часто расходится с тобой. Этот пункт всё равно решает исход: "
    "здесь опора — проверки и ты."
)
TODAY_LINE = "Сегодня: {used} из {cap}."
RATE_BUTTON = "Оценить ответ"

CARD_TEXT = "Мой ответ от {when}:\n\n«{reply}»\n\nЧто здесь не так?"
ALL_FINE_BUTTON = "Всё в порядке"
DONE_BUTTON = "Готово"
SKIP_BUTTON = "Пропустить"
MORE_BUTTON = "Ещё один"
# Room for the card's own lines inside Telegram's 4096.
REPLY_MAX = 3500

RESULT_TEXT = "Сохранил. Судья согласен с тобой в {agreed} из {total}."
RESULT_DISAGREED = "Расходитесь: {labels}."
RESULT_UNUSABLE = "Сохранил твои отметки. Судья не дал пригодной оценки."

NOTHING_TEXT = "Нет ответов за 14 дней, которые ещё не оценены."
DAY_CAP_TEXT = "На сегодня хватит: {cap} оценок в день (JUDGE_LABELS_PER_DAY)."
SPEND_CAP_TEXT = "Дневной лимит расходов исчерпан. Попробуй завтра."
DEPENDENCE_TEXT = {
    llm_models.NO_JUDGE: "Судья не задан — сверять не с кем.",
    llm_models.SAME_MODEL: "Судья — та же модель, что у чата. Сверка не идёт, пока судью не сменят.",
    llm_models.SAME_VENDOR: (
        "Судья того же разработчика, что и чат. Сверка не идёт, пока судью не сменят."
    ),
}
STALE_TEXT = "Устарело. /judge"

NOTICE_ADVISORY = (
    "Судья часто расходится с тобой: {labels} — больше не решает исход проверок поправок "
    "и канарейки, только отмечается. /judge"
)
NOTICE_RELIABLE = "Судья снова решает исход по пунктам: {labels}. /judge"


def _labels(items) -> str:
    return ", ".join(ITEM_LABELS[item] for item in items)


def refusal_text(code: str, settings: Settings) -> str:
    if code == calibration.DAY_CAP:
        return DAY_CAP_TEXT.format(cap=settings.JUDGE_LABELS_PER_DAY)
    if code == calibration.SPEND_CAP:
        return SPEND_CAP_TEXT
    if code in (calibration.NOTHING, calibration.GONE):
        return NOTHING_TEXT if code == calibration.NOTHING else STALE_TEXT
    return DEPENDENCE_TEXT.get(code, DEPENDENCE_TEXT[llm_models.NO_JUDGE])


# --- the status ------------------------------------------------------------------


def rate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=RATE_BUTTON, callback_data=f"{PREFIX}:nx")]]
    )


async def status(sessionmaker, settings: Settings, clock: Clock) -> tuple[str, InlineKeyboardMarkup | None]:
    """`/judge`'s text and keyboard. The button is offered only while a
    round could run; otherwise the reason stands in for it."""
    async with sessionmaker() as session:
        state = await get_state(session)
        stats = await calibration.agreement(session, settings)
        used = await calibration.rounds_today(session, clock, state.timezone)
        why = await calibration.refusal(session, settings, clock, state.timezone)
    lines = [STATUS_HEADER.format(model=llm_models.effective_model(settings, "judge") or "—")]
    if not any(item.count for item in stats.values()):
        lines.append(STATUS_EMPTY)
    for item in calibration.CORE_ITEMS:
        item_stats = stats[item]
        if not item_stats.count:
            lines.append(STATUS_LINE_EMPTY.format(label=ITEM_LABELS[item]))
            continue
        line = STATUS_LINE.format(
            label=ITEM_LABELS[item],
            count=item_stats.count,
            pct=round(100 * (item_stats.agreement or 0)),
            both_ok=item_stats.both_ok,
            both_bad=item_stats.both_bad,
            only_user=item_stats.only_user,
            only_judge=item_stats.only_judge,
        )
        unreliable = calibration.is_unreliable(item_stats, settings)
        if unreliable and item != calibration.NEVER_ADVISORY:
            line += ADVISORY_MARK
        lines.append(line)
    if calibration.is_unreliable(stats[calibration.NEVER_ADVISORY], settings):
        lines.append(BOUNDARIES_WARNING)
    lines.append(TODAY_LINE.format(used=used, cap=settings.JUDGE_LABELS_PER_DAY))
    if why is not None:
        lines.append(refusal_text(why, settings))
        return "\n".join(lines), None
    return "\n".join(lines), rate_keyboard()


# --- the card --------------------------------------------------------------------


def card_keyboard(message_id: int, mask: int, issued_at: int) -> InlineKeyboardMarkup:
    def data(action: str) -> str:
        return f"{PREFIX}:{action}:{message_id}:{mask}:{issued_at}"

    toggles = [
        InlineKeyboardButton(
            text=("❌ " if mask & (1 << i) else "▫️ ") + ITEM_LABELS[item],
            callback_data=data(f"t{i}"),
        )
        for i, item in enumerate(calibration.CORE_ITEMS)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            toggles[:2],
            toggles[2:4],
            toggles[4:],
            [
                InlineKeyboardButton(text=ALL_FINE_BUTTON, callback_data=data("ok")),
                InlineKeyboardButton(text=DONE_BUTTON, callback_data=data("go")),
            ],
            [InlineKeyboardButton(text=SKIP_BUTTON, callback_data=data("sk"))],
        ]
    )


def card_text(reply, timezone: str) -> str:
    content = reply.content
    if len(content) > REPLY_MAX:
        content = content[:REPLY_MAX].rstrip() + "…"
    when = f"{reply.created_at.astimezone(zone_of(timezone)):%d.%m %H:%M}"
    return CARD_TEXT.format(when=when, reply=content)


async def new_card(
    sessionmaker, settings: Settings, clock: Clock, *, exclude: tuple[int, ...] = ()
) -> tuple[str, InlineKeyboardMarkup | None]:
    """A fresh card, or the reason there is none."""
    async with sessionmaker() as session:
        state = await get_state(session)
        why = await calibration.refusal(session, settings, clock, state.timezone)
        if why is not None:
            return refusal_text(why, settings), None
        reply = await calibration.pick_reply(session, clock, exclude=exclude)
        if reply is None:
            return NOTHING_TEXT, None
        text = card_text(reply, state.timezone)
        message_id = reply.id
    return text, card_keyboard(message_id, 0, int(clock.now_utc().timestamp()))


def result_text(result: calibration.RoundResult) -> str:
    if not result.usable:
        return RESULT_UNUSABLE
    text = RESULT_TEXT.format(agreed=result.agreed, total=result.total)
    if result.disagreed:
        text += "\n" + RESULT_DISAGREED.format(labels=_labels(result.disagreed))
    return text


def more_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=MORE_BUTTON, callback_data=f"{PREFIX}:nx")]]
    )


async def run_command(sessionmaker, bot: Bot, settings: Settings, clock: Clock, *, chat_id: int) -> None:
    """`/judge`: the status message with «Оценить ответ»."""
    text, keyboard = await status(sessionmaker, settings, clock)
    await send_keyboard(bot, chat_id, text, keyboard)


def _parse(data: str) -> tuple[str, int, int, int] | None:
    parts = data.split(":")
    if len(parts) != 5 or parts[0] != PREFIX:
        return None
    _, action, raw_id, raw_mask, raw_issued = parts
    try:
        message_id, mask, issued_at = int(raw_id), int(raw_mask), int(raw_issued)
    except ValueError:
        return None
    if message_id <= 0 or not 0 <= mask < (1 << len(calibration.CORE_ITEMS)):
        return None
    return action, message_id, mask, issued_at


async def handle_callback(
    sessionmaker,
    bot: Bot,
    settings: Settings,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
    judge_provider: LLMProvider | None = None,
) -> None:
    """Every `jl:` press. `jl:nx` (from the status and the result) edits
    the message into a new card. On a card: `t<i>` toggles item i, `sk`
    shows another reply, `ok` submits with nothing marked, `go` with the
    marks; a forged, malformed or stale press answers «Устарело.»."""
    if data == f"{PREFIX}:nx":
        await answer_callback(bot, callback_id)
        text, keyboard = await new_card(sessionmaker, settings, clock)
        await edit_keyboard(bot, chat_id, message_id, text, keyboard)
        return

    parsed = _parse(data)
    now = clock.now_utc().timestamp()
    if parsed is None or not 0 <= now - parsed[3] <= CARD_TTL:
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, STALE_TEXT, None)
        return
    action, reply_id, mask, issued_at = parsed

    if action.startswith("t") and action[1:].isdigit() and int(action[1:]) < len(calibration.CORE_ITEMS):
        await answer_callback(bot, callback_id)
        mask ^= 1 << int(action[1:])
        # Only the keyboard changes; the quoted text is re-sent as it is
        # on the message (edit_keyboard needs a text), read back from
        # the database rather than carried anywhere.
        async with sessionmaker() as session:
            state = await get_state(session)
            reply = await calibration.eligible_reply(session, clock, reply_id)
            text = card_text(reply, state.timezone) if reply is not None else None
        if text is None:
            await edit_keyboard(bot, chat_id, message_id, STALE_TEXT, None)
            return
        await edit_keyboard(bot, chat_id, message_id, text, card_keyboard(reply_id, mask, issued_at))
        return

    if action == "sk":
        await answer_callback(bot, callback_id)
        text, keyboard = await new_card(sessionmaker, settings, clock, exclude=(reply_id,))
        await edit_keyboard(bot, chat_id, message_id, text, keyboard)
        return

    if action not in ("ok", "go"):
        await answer_callback(bot, callback_id)
        await edit_keyboard(bot, chat_id, message_id, STALE_TEXT, None)
        return

    flagged = frozenset() if action == "ok" else frozenset(
        item for i, item in enumerate(calibration.CORE_ITEMS) if mask & (1 << i)
    )
    await answer_callback(bot, callback_id)
    async with sessionmaker() as session:
        state = await get_state(session)
        outcome = await calibration.submit(
            session, settings, clock,
            message_id=reply_id, flagged=flagged, timezone=state.timezone,
            judge_provider=judge_provider,
        )
    if isinstance(outcome, str):
        await edit_keyboard(bot, chat_id, message_id, refusal_text(outcome, settings), None)
        return
    await edit_keyboard(bot, chat_id, message_id, result_text(outcome), more_keyboard())


# --- the notice ------------------------------------------------------------------


def notice_text(change: calibration.Transition) -> str:
    lines = []
    if change.advisory:
        lines.append(NOTICE_ADVISORY.format(labels=_labels(change.advisory)))
    if change.reliable:
        lines.append(NOTICE_RELIABLE.format(labels=_labels(change.reliable)))
    return "\n".join(lines)


async def send_notice(sessionmaker, bot: Bot, settings: Settings, clock: Clock) -> bool:
    """One message for whatever changed since the last notice, if the bot
    may speak now (`may_report_now`); otherwise nothing, and the next
    call tries again. Recorded right after the send, so a send that
    fails is retried and a restart repeats nothing. True if sent."""
    async with sessionmaker() as session:
        change = await calibration.transition(session, settings)
        if not change:
            return False
        state = await get_state(session)
        if not may_report_now(settings, clock, state):
            return False
        chat_id = state.chat_id
    if chat_id is None:
        return False
    await bot.send_message(chat_id=chat_id, text=notice_text(change))
    async with sessionmaker() as session:
        await calibration.mark_announced(session, clock, change)
    return True


__all__ = [
    "CARD_TTL",
    "ITEM_LABELS",
    "PREFIX",
    "card_keyboard",
    "handle_callback",
    "new_card",
    "notice_text",
    "run_command",
    "send_notice",
    "status",
]
