"""Text, keyboards and callbacks for the planner commands (P2 read path,
P3 `/task`/`/event`/`/done` and their buttons).

Out of character throughout, like app/tg/proposals.py's confirmation
messages: this is the bot reporting on a system, not Anchor talking.
The persona-voiced version of "what's today" lives in the now-block
(app/core/prompt.py) and in the MORNING message.

**Confirm cards** (`/task`, `/event`) follow proposals.py's shape
exactly: send_confirm_card -> a message with `pa:y:<id>`/`pa:n:<id>`
buttons -> handle_confirm_callback edits it in place and, on accept,
leaves the actual write to the PLANNER_WRITE job (app/planner/jobs.py),
which sends its own follow-up once the write has actually happened --
this module never claims the write is done before it is.

**`/done`'s buttons** (`pl:d:<task id>`) are not a confirm card: picking
a specific task off a list the user was just shown *is* the
confirmation (see app/planner/actions.py's module docstring), so
handle_done_callback creates and accepts the action in the same call.
"""

from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.core.clock import Clock
from app.db.models import PlannerCredential
from app.planner import actions as planner_actions
from app.planner import snapshot as planner_snapshot
from app.tg.send import answer_callback, edit_keyboard, send_keyboard

logger = logging.getLogger(__name__)

DISABLED = "Планер выключен на этом сервере."
NOT_LINKED = "Планер не подключён. Набери /planner_link, чтобы подключить."
REVOKED = "Доступ к планеру отозван. Набери /planner_link, чтобы подключить заново."
PAUSED = "Планер подключён, но выключен (/planner off). Набери /planner on, чтобы включить."

PLAN_HEADER = "План на сегодня"
PLAN_EMPTY = "Пока пусто — либо план и правда пуст, либо данные ещё не синхронизировались."

LINK_INTRO = (
    "Открой ссылку в браузере, где ты уже вошёл в планер, и разреши доступ:\n{url}\n"
    "Ссылка действует 10 минут."
)
LINK_DISABLED = "Планер выключен на этом сервере — /planner_link не нужен."
LINK_ALREADY = "Планер уже подключён. Сначала отключи текущий доступ на стороне планера, если хочешь перевыпустить его."

LINKED_OK = "Планер подключён."
LINK_FAILED = "Не получилось подключить планер: {reason}"

ON_REPLY = "Планер включён."
OFF_REPLY = "Планер выключен."
ON_OFF_NOT_LINKED = "Сначала подключи планер: /planner_link."
ON_OFF_USAGE = "Как именно? /planner on или /planner off."

STATUS_HEADER = "Планер"

# --- P3: /task, /event ---------------------------------------------------

TASK_USAGE = (
    "Что за задача? /task <название> [на <дата>].\n"
    "Например: /task Купить молоко на завтра"
)
EVENT_USAGE = (
    "Что за событие? /event <название> в <ЧЧ:ММ> [дата] [на <длительность>].\n"
    "Например: /event Встреча с Аней в 18:00 завтра на 2 ч"
)

WRITE_CAP_REACHED = (
    "Сегодня уже набралось {count} записей в планер (максимум {cap} в день). "
    "Попробуй завтра."
)

CONFIRM_YES = "Добавить"
CONFIRM_NO = "Отмена"
ACTION_ACCEPTED = "✅ Принято, записываю…"
ACTION_REJECTED = "✖️ Отменено"
ACTION_STALE = "Устарело."

# --- P3: /done -------------------------------------------------------

DONE_HEADER = "Какую задачу отметить сделанной?"
DONE_EMPTY = "Открытых задач нет — либо всё сделано, либо план ещё не синхронизировался."
DONE_STALE = "Данные устарели. Набери /plan, чтобы обновить, и попробуй снова."
DONE_MARKED = "Отмечено: «{title}»."
DONE_TITLE_MAX = 40


def render_plan_text(
    snapshot,
    clock: Clock,
    timezone: str,
    *,
    max_age_min: int,
) -> str:
    lines = planner_snapshot.render_lines(snapshot, clock, timezone, max_age_min=max_age_min)
    if not lines:
        return f"{PLAN_HEADER}\n{PLAN_EMPTY}"
    return "\n".join([PLAN_HEADER, *lines])


def render_status_text(
    credential: PlannerCredential | None,
    snapshot,
    clock: Clock,
    timezone: str,
    *,
    max_age_min: int,
) -> str:
    if credential is None:
        return f"{STATUS_HEADER}: не подключён."

    status_label = "активен" if credential.status == "active" else "отозван"
    toggle_label = "включён" if credential.enabled else "выключен (/planner on)"

    lines = [f"{STATUS_HEADER}: {status_label}, {toggle_label}."]
    if planner_snapshot.is_stale(snapshot, clock, max_age_min):
        lines.append("Последняя синхронизация устарела или её ещё не было.")
    else:
        stamp = snapshot.fetched_at.astimezone(ZoneInfo(timezone)).strftime("%H:%M")
        lines.append(f"Обновлено сегодня в {stamp}.")
    return "\n".join(lines)


# --- P3: confirm cards for /task and /event -------------------------------


def _clip(title: str | None, limit: int = DONE_TITLE_MAX) -> str:
    title = (title or "").strip() or "(без названия)"
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def confirm_task_text(payload: dict) -> str:
    due = payload.get("due_date")
    due_line = f"\nСрок: {due}" if due else ""
    return f"Добавить в планер?\nЗадача: «{payload['title']}»{due_line}"


def _fmt_event_when(payload: dict, timezone: str) -> str:
    tz = ZoneInfo(timezone)
    start = datetime.datetime.fromisoformat(payload["start"]).astimezone(tz)
    if payload.get("all_day"):
        return f"{start.strftime('%d.%m')}, весь день"
    end = datetime.datetime.fromisoformat(payload["end"]).astimezone(tz)
    if start.date() == end.date():
        return f"{start.strftime('%d.%m')} {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
    return f"{start.strftime('%d.%m %H:%M')} – {end.strftime('%d.%m %H:%M')}"


def confirm_event_text(payload: dict, timezone: str) -> str:
    return f"Добавить в планер?\nСобытие: «{payload['title']}»\n{_fmt_event_when(payload, timezone)}"


def _confirm_text(action: planner_actions.PlannerAction, timezone: str) -> str:
    if action.kind == planner_actions.CREATE_TASK:
        return confirm_task_text(action.payload)
    return confirm_event_text(action.payload, timezone)


def confirm_keyboard(action_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=CONFIRM_YES, callback_data=f"pa:y:{action_id}"),
                InlineKeyboardButton(text=CONFIRM_NO, callback_data=f"pa:n:{action_id}"),
            ]
        ]
    )


async def send_confirm_card(
    sessionmaker, bot: Bot, *, chat_id: int, action_id: int, timezone: str
) -> None:
    """Send the `pa:y|n:<id>` card for a freshly created, still-pending action."""
    async with sessionmaker() as session:
        action = await planner_actions.get(session, action_id)
        if action is None or action.status != planner_actions.PENDING:
            return
        text = _confirm_text(action, timezone)

    message_id = await send_keyboard(bot, chat_id, text, confirm_keyboard(action_id))
    async with sessionmaker() as session:
        await planner_actions.set_message_id(session, action_id, message_id)


async def handle_confirm_callback(
    sessionmaker,
    bot: Bot,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
    timezone: str,
) -> None:
    """`pa:y:<id>` / `pa:n:<id>`.

    Idempotent by construction, like app/tg/proposals.py's decision
    callback: accept()/reject() return None for a row that is not
    pending, and a replayed press then just strips the buttons without
    enqueueing a second PLANNER_WRITE.
    """
    _, decision, raw_id = data.split(":", 2)
    await answer_callback(bot, callback_id)

    try:
        action_id = int(raw_id)
    except ValueError:
        await edit_keyboard(bot, chat_id, message_id, ACTION_STALE, None)
        return

    async with sessionmaker() as session:
        action = await planner_actions.get(session, action_id)
        base_text = _confirm_text(action, timezone) if action is not None else None
        if decision == "y":
            decided = await planner_actions.accept(session, clock, action_id)
        else:
            decided = await planner_actions.reject(session, clock, action_id)

    if decided is None:
        text = f"{base_text}\n{ACTION_STALE}" if base_text else ACTION_STALE
        await edit_keyboard(bot, chat_id, message_id, text, None)
        return

    outcome = ACTION_ACCEPTED if decision == "y" else ACTION_REJECTED
    await edit_keyboard(bot, chat_id, message_id, f"{base_text}\n{outcome}", None)


# --- P3: /done -------------------------------------------------------------


def render_done_list(
    snapshot, clock: Clock, timezone: str, *, max_age_min: int
) -> tuple[str, list[dict]]:
    """The `/done` reply text plus the open tasks to build buttons from.

    `[]` (with DONE_STALE) for a missing/stale snapshot, same rule
    app/planner/snapshot.py's render_lines uses -- /done must not offer
    to complete a task from data that might already be wrong.
    """
    if planner_snapshot.is_stale(snapshot, clock, max_age_min):
        return DONE_STALE, []
    tasks = [t for t in (snapshot.payload or {}).get("tasks", []) if t.get("id")]
    if not tasks:
        return DONE_EMPTY, []
    return DONE_HEADER, tasks


def done_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ {_clip(task.get('title'))}", callback_data=f"pl:d:{task['id']}")]
            for task in tasks
        ]
    )


def _task_title(snapshot, task_id: str) -> str | None:
    if snapshot is None:
        return None
    for task in (snapshot.payload or {}).get("tasks", []):
        if task.get("id") == task_id:
            return task.get("title")
    return None


async def handle_done_callback(
    sessionmaker,
    bot: Bot,
    settings: Settings,
    clock: Clock,
    *,
    callback_id: str,
    chat_id: int,
    message_id: int,
    data: str,
    timezone: str,
) -> None:
    """`pl:d:<task id>` -- see the module docstring: the tap is itself
    the confirmation, so this creates and accepts the action in one go.

    Still honours `PLANNER_MAX_WRITES_PER_DAY`: a day's cap is spent on
    every accepted write, and a /done tap is one, same as /task or
    /event (app/planner/actions.py's count_today docstring).
    """
    task_id = data[len("pl:d:") :]
    await answer_callback(bot, callback_id)

    async with sessionmaker() as session:
        snap = await planner_snapshot.get_snapshot(session)
        title = _task_title(snap, task_id)
        count = await planner_actions.count_today(session, clock, timezone)
        if count >= settings.PLANNER_MAX_WRITES_PER_DAY:
            text = WRITE_CAP_REACHED.format(count=count, cap=settings.PLANNER_MAX_WRITES_PER_DAY)
            await edit_keyboard(bot, chat_id, message_id, text, None)
            return
        action = await planner_actions.create(
            session, clock, kind=planner_actions.COMPLETE_TASK, payload={"task_id": task_id, "title": title}
        )
        await planner_actions.accept(session, clock, action.id)

    await edit_keyboard(bot, chat_id, message_id, DONE_MARKED.format(title=title or task_id), None)


__all__ = [
    "DISABLED",
    "NOT_LINKED",
    "REVOKED",
    "PAUSED",
    "LINK_INTRO",
    "LINK_DISABLED",
    "LINK_ALREADY",
    "LINKED_OK",
    "LINK_FAILED",
    "ON_REPLY",
    "OFF_REPLY",
    "ON_OFF_NOT_LINKED",
    "ON_OFF_USAGE",
    "TASK_USAGE",
    "EVENT_USAGE",
    "WRITE_CAP_REACHED",
    "DONE_HEADER",
    "DONE_EMPTY",
    "DONE_STALE",
    "DONE_MARKED",
    "render_plan_text",
    "render_status_text",
    "confirm_task_text",
    "confirm_event_text",
    "confirm_keyboard",
    "send_confirm_card",
    "handle_confirm_callback",
    "render_done_list",
    "done_keyboard",
    "handle_done_callback",
]
