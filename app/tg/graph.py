"""/graph: footer link pruning (vaultd/vaultd/prune.py), from Telegram.

- `/graph` runs a dry run now: the report goes out as a Markdown
  document, followed by one message with the counts and, when there is
  something to remove, «применить» (`gp:a:<plan hash>`).
- `/graph apply <hash>` (or the button) applies exactly the plan with
  that hash. vaultd recomputes it under its lock and refuses
  (`plan_changed`) when any note changed since: the reply then asks for
  a fresh `/graph`. Applied, the message offers «отменить»
  (`gp:u:<changeset>`).
- `/graph undo` takes back the newest pruning still undoable (14 days,
  vaultd's undo store), the button the one it names. Both go through
  vaultd's writer-scoped undo (`writer=echo`), so they never touch a
  Claude write or an inbox note: only a changeset whose purpose is
  `prune` is ever named.

The weekly dry run (`run_weekly`, queued by app/core/scheduler.py's
`maybe_enqueue_graph_prune`) sends the same report and message, held by
`may_report_now` like the library digest, and skipped when there is
nothing to remove and nothing broken to report.

Telegram only (app/web/ingress.py's BLOCKED_COMMANDS, the router's
is_web_sink guard): applying edits the vault. The report carries note
titles; it goes to Telegram only. Logs carry counts and outcome codes.
"""

from __future__ import annotations

import datetime
import logging
import re
import secrets
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core import clock as clock_module
from app.core.clock import Clock
from app.core.report import may_report_now
from app.core.scene import Deferred
from app.core.state import get_state
from app.tg.send import answer_callback, edit_keyboard, send_document
from app.vault import errors
from app.vault.client import PrunePlan, VaultClient
from app.vault.errors import VaultError

logger = logging.getLogger(__name__)

HELP = (
    "/graph — сухой прогон: какие ссылки в футерах «## Связи» можно убрать (ничего не меняет)\n"
    "/graph apply ХЕШ — применить план с этим хешем\n"
    "/graph undo — отменить последнее применение (до 14 дней)"
)
OFF = "Чистка графа выключена: нужно хранилище (VAULT_MODE) и GRAPH_PRUNE_ENABLED."
UNAVAILABLE = "Хранилище не ответило. Попробуй позже."
TOO_LARGE = "Заметок или правок слишком много для одного прогона — ничего не сделано."
CONFIG_ERROR = "Anchor/graph.md не читается: {error}. Ничего не предложено."
SUMMARY = (
    "Граф ссылок: заметок {notes}, связей {before} → {after}. "
    "Убрать из футеров: {removals} (заметок: {files}). Ссылок в никуда: {unresolved}, сирот: {orphans}."
)
NOTHING = "Граф ссылок: убирать нечего. Ссылок в никуда: {unresolved}, сирот: {orphans}."
APPLY_HINT = "Отчёт выше. Применить: кнопка или /graph apply {hash}"
APPLY_BUTTON = "Применить"
UNDO_BUTTON = "Отменить"
APPLIED = "Готово: убрано связей {removed}, изменено заметок {files}. Отменить: /graph undo"
APPLIED_NOTHING = "Применять нечего: план пуст."
PLAN_CHANGED = "План устарел: заметки менялись после прогона. Запусти /graph ещё раз."
APPLY_REFUSED = "Хранилище отказало. Ничего не изменено."
APPLY_USAGE = "/graph apply ХЕШ (хеш — из сообщения после /graph)"
UNDONE = "Отменено: восстановлено заметок {restored}."
UNDONE_PARTLY = "Отменено частично: восстановлено {restored}, не тронуто {refused} (их меняли после чистки)."
NOTHING_TO_UNDO = "Отменять нечего: чисток за 14 дней нет."
UNDO_GONE = "Эту чистку уже не отменить: её нет в хранилище или она уже отменена."
STALE = "Устарело."

_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
_CALLBACK_RE = re.compile(r"^gp:(a:[0-9a-f]{32}|u:prune-[0-9a-f]{16})$")
REPORT_NAME = "graph-{date}.md"


@dataclass(frozen=True)
class Reply:
    text: str
    keyboard: InlineKeyboardMarkup | None = None
    document: bytes | None = None
    filename: str | None = None


def enabled(settings: Settings) -> bool:
    return settings.GRAPH_PRUNE_ENABLED and settings.VAULT_MODE != "off"


def _new_changeset() -> str:
    return "prune-" + secrets.token_hex(8)


def apply_keyboard(plan_hash: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=APPLY_BUTTON, callback_data=f"gp:a:{plan_hash}")]]
    )


def undo_keyboard(changeset: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=UNDO_BUTTON, callback_data=f"gp:u:{changeset}")]]
    )


def render(plan: PrunePlan, local_date: datetime.date) -> Reply:
    """The dry run's Telegram reply: the report as a document, the counts
    as text, and the button when there is something to apply."""
    document = plan.report.encode("utf-8")
    filename = REPORT_NAME.format(date=local_date.isoformat())
    if plan.config_error is not None:
        return Reply(CONFIG_ERROR.format(error=plan.config_error), None, document, filename)
    if plan.removals == 0:
        return Reply(NOTHING.format(unresolved=plan.unresolved, orphans=plan.orphans), None, document, filename)
    text = SUMMARY.format(
        notes=plan.notes,
        before=plan.edges_before,
        after=plan.edges_after,
        removals=plan.removals,
        files=plan.files,
        unresolved=plan.unresolved,
        orphans=plan.orphans,
    )
    text += "\n" + APPLY_HINT.format(hash=plan.plan_hash)
    return Reply(text, apply_keyboard(plan.plan_hash), document, filename)


async def _fetch(settings: Settings, client_factory) -> PrunePlan | Reply:
    try:
        plan = await client_factory(settings).graph_prune_plan()
    except VaultError as exc:
        logger.warning("graph prune dry run failed", extra={"error_code": exc.code})
        return Reply(TOO_LARGE if exc.code == errors.REFUSED else UNAVAILABLE)
    logger.info("graph prune dry run", extra={"event": "graph_prune", "count": plan.removals})
    return plan


async def dry_run(settings: Settings, clock: Clock, timezone: str, client_factory) -> Reply:
    plan = await _fetch(settings, client_factory)
    if isinstance(plan, Reply):
        return plan
    return render(plan, clock_module.local_date(clock, timezone))


async def apply(settings: Settings, plan_hash: str, client_factory) -> Reply:
    changeset = _new_changeset()
    try:
        result = await client_factory(settings).graph_prune_apply(plan_hash, changeset)
    except VaultError as exc:
        logger.warning("graph prune apply failed", extra={"error_code": exc.code})
        if exc.code == errors.CONFLICT:
            return Reply(PLAN_CHANGED)
        if exc.code == errors.REFUSED:
            return Reply(APPLY_REFUSED)
        return Reply(UNAVAILABLE)
    logger.info("graph prune applied", extra={"event": "graph_prune", "count": result.files})
    if result.files == 0:
        return Reply(APPLIED_NOTHING)
    return Reply(APPLIED.format(removed=result.removed, files=result.files), undo_keyboard(changeset))


async def undo(settings: Settings, client_factory, changeset: str | None = None) -> Reply:
    """Undo `changeset`, or the newest pruning not yet undone."""
    client = client_factory(settings)
    try:
        changes = [
            c
            for c in await client.list_changes()
            if c.writer == "echo" and c.purpose == "prune" and c.kind == "write" and not c.undone
        ]
        if changeset is None:
            if not changes:
                return Reply(NOTHING_TO_UNDO)
            changeset = max(changes, key=lambda c: c.time).id
        elif changeset not in {c.id for c in changes}:
            return Reply(UNDO_GONE)
        result = await client.undo_changeset(changeset, writer="echo")
    except VaultError as exc:
        logger.warning("graph prune undo failed", extra={"error_code": exc.code})
        if exc.code == errors.NOT_FOUND:
            return Reply(UNDO_GONE)
        return Reply(APPLY_REFUSED if exc.code == errors.REFUSED else UNAVAILABLE)
    logger.info("graph prune undone", extra={"event": "graph_prune", "count": result.restored})
    if result.refused:
        return Reply(UNDONE_PARTLY.format(restored=result.restored, refused=result.refused))
    return Reply(UNDONE.format(restored=result.restored))


async def command(
    sessionmaker, settings: Settings, clock: Clock, args: str | None, client_factory=VaultClient.from_settings
) -> Reply:
    words = (args or "").split()
    if words and words[0].lower() in ("help", "?"):
        return Reply(HELP)
    if not enabled(settings):
        return Reply(OFF)
    if not words:
        async with sessionmaker() as session:
            state = await get_state(session)
        return await dry_run(settings, clock, state.timezone, client_factory)
    verb = words[0].lower()
    if verb == "apply":
        if len(words) != 2 or not _HASH_RE.match(words[1].lower()):
            return Reply(APPLY_USAGE)
        return await apply(settings, words[1].lower(), client_factory)
    if verb == "undo" and len(words) == 1:
        return await undo(settings, client_factory)
    return Reply(HELP)


async def send(bot: Bot, chat_id: int, reply: Reply) -> int | None:
    """Send a reply: the document first (when there is one), then the text."""
    if reply.document is not None:
        await send_document(bot, chat_id, reply.document, reply.filename or "graph.md")
    message = await bot.send_message(chat_id=chat_id, text=reply.text, reply_markup=reply.keyboard)
    return message.message_id


async def handle_callback(
    settings: Settings,
    bot: Bot,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    message_text: str | None,
    data: str | None,
    client_factory=VaultClient.from_settings,
) -> None:
    """A press on «применить» or «отменить». The message is edited in place:
    its text gains the outcome, and the keyboard is replaced (undo after
    an apply) or removed."""
    if data is None or not _CALLBACK_RE.match(data):
        await answer_callback(bot, callback_id, STALE)
        return
    if not enabled(settings):
        await answer_callback(bot, callback_id, OFF)
        return
    action, value = data[3], data[5:]
    reply = await (apply(settings, value, client_factory) if action == "a" else undo(settings, client_factory, value))
    await answer_callback(bot, callback_id)
    text = ((message_text or "").strip() + "\n\n" + reply.text).strip()
    await edit_keyboard(bot, chat_id, message_id, text, reply.keyboard)


async def run_weekly(session, settings: Settings, clock: Clock, bot: Bot, client_factory=VaultClient.from_settings) -> None:
    """The weekly dry run job (module docstring). Raises Deferred while
    reports are held; an unreachable vault is retried the same way."""
    if not enabled(settings):
        return
    state = await get_state(session)
    if state.chat_id is None:
        return
    if not may_report_now(settings, clock, state):
        raise Deferred(clock.now_utc() + RETRY)
    plan = await _fetch(settings, client_factory)
    if isinstance(plan, Reply):
        raise Deferred(clock.now_utc() + RETRY)
    quiet = plan.config_error is None and not (plan.removals or plan.unresolved or plan.orphans)
    if quiet:
        return  # nothing to remove and nothing broken: no message
    await send(bot, state.chat_id, render(plan, clock_module.local_date(clock, state.timezone)))


RETRY = datetime.timedelta(minutes=30)
