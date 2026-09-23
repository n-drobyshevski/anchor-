"""Text for `/plan`, `/planner` and `/planner_link` (P2, read path only).

Out of character, like every other command reply and like
app/tg/proposals.py's confirmation messages: this is the bot reporting
on a system, not Anchor talking. The persona-voiced version of "what's
today" lives in the now-block (app/core/prompt.py) and in the MORNING
message; these commands are the deterministic, always-the-same-shape
counterpart the design review calls for.

`/task`, `/event`, `/done` and the `pa:*`/`pl:d:*` callbacks are P3 and
are not here yet -- see the plan's phase order.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.core.clock import Clock
from app.db.models import PlannerCredential
from app.planner import snapshot as planner_snapshot

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
    "render_plan_text",
    "render_status_text",
]
