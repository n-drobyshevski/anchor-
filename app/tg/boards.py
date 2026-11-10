"""`/boards`: the task boards kept in step with the planner (board sync).

Out of character, like the other planner and vault commands: this is the
bot reporting on a system. Telegram only (app/web/ingress.py's
BLOCKED_COMMANDS), like `/graph`: `now` and `undo` change the vault.

- `/boards` -- status from stored state only (`board_pair`,
  `board_sync_run`): per board, how many cards are paired and how many
  are held, the last pass's time and outcome, its conflicts and skipped
  titles; and what is not set up (the flag, the vault, the planner, no
  board named in `Anchor/settings.md`). No call to anything.
- `/boards preview` -- a dry run: queues a pass that reads and computes
  and writes nothing anywhere, and replies with its counts (cards, tasks,
  subtasks, dependencies). Works with
  `BOARD_SYNC_ENABLED` off: it is how the first pairing is checked before
  the flag goes on.
- `/boards now` -- one pass right away (needs the flag).
- `/boards undo` -- the last board write taken back through vaultd's
  undo, one level, like `/flows notes undo`. The planner side of that
  pass is not undone, and the boards it held are forgotten so the next
  pass pairs them again by title rather than reading the restored cards
  as deleted ones.

`preview` and `now` are jobs (app/planner/board_job.py), not calls made
here: the planner client lives on the job path, never on the chat path
(app/planner/client.py's own rule), and the worker sends the result.

Every text here carries counts, codes and the board's own file name --
which goes to the user, never to a log.
"""

from __future__ import annotations

import datetime

from app.config import Settings
from app.core import clock as clock_module
from app.core import echo_write
from app.core.clock import Clock
from app.core.state import get_state
from app.planner import auth as planner_auth
from app.planner import board_job
from app.planner.board_sync import Counts

HEADER = "Доски ⇄ планер"
USAGE = "Как именно? /boards, /boards preview, /boards now или /boards undo."
PLANNER_OFF = "Планер выключен на этом сервере — доскам не с чем синхронизироваться."
NOT_LINKED = "Планер не подключён или выключен (/planner) — доскам не с чем синхронизироваться."
VAULT_OFF = "Хранилище выключено (VAULT_MODE=off) — до досок не достать."
FLAG_OFF = (
    "Фоновая синхронизация выключена (BOARD_SYNC_ENABLED). "
    "/boards preview покажет, что сделал бы проход, ничего не записывая."
)
FLAG_ON = "Фоновая синхронизация: вкл, после каждого обновления планера, не чаще раза в 15 минут."
NO_BOARDS = (
    "Ни одна доска не названа: впиши её в task_boards в Anchor/settings.md "
    "(и в BOARD_SYNC_TAGS, если имя не «Work tasks.md» или «General tasks.md»)."
)
NEVER_RAN = "проходов ещё не было"
PREVIEW_QUEUED = "Считаю пробный проход — ничего не запишу. Пришлю итог."
NOW_QUEUED = "Запускаю проход. Пришлю итог."
ALREADY_QUEUED = "Проход уже в очереди. Пришлю итог."

OUTCOMES = {
    board_job.OK: "готово",
    board_job.CHANGED: "доску меняли во время прохода, повторю в следующий раз",
    board_job.UNAVAILABLE: "хранилище не ответило",
    board_job.REFUSED: "хранилище отказало в записи",
    board_job.PLANNER_UNAVAILABLE: "планер не ответил",
    board_job.NO_BOARDS: "в Anchor/settings.md не названо ни одной доски из BOARD_SYNC_TAGS",
    board_job.MISSING: "файла доски нет",
    board_job.NOT_BOARD: "файл не доска Kanban Starlane",
}

RESULT_GATES = {
    board_job.DISABLED: FLAG_OFF,
    board_job.VAULT_OFF: VAULT_OFF,
    board_job.NOT_LINKED: NOT_LINKED,
}

UNDO_REPLIES = {
    echo_write.UNDONE: (
        "Последняя запись в доски отменена. Планер не откатывается: созданные, "
        "изменённые и лишённые тега задачи остались как есть. Пары этих досок "
        "забыты — следующий проход соберёт их заново по названиям и может снова "
        "внести те же изменения; выключи BOARD_SYNC_ENABLED, если не хочешь этого."
    ),
    echo_write.NOTHING: "Отменять нечего: последней записи в доски нет или она уже отменена.",
    echo_write.EXPIRED: "Эта запись старше 14 дней — хранилище её уже не помнит.",
    echo_write.CHANGED: "Доски меняли после этой записи — отмена ничего не тронула.",
    echo_write.REFUSED: "Хранилище отказало в отмене (например, лимит отмен в час). Попробуй позже.",
    echo_write.UNAVAILABLE: "Хранилище не ответило. Попробуй позже.",
}

# Digest: one line when the day's passes met a conflict or a title they
# could not carry. Counts only.
DIGEST_LINE = "Доски: за сутки конфликтов {conflicts}, пропущенных названий {skipped} — /boards."


def _when(at: datetime.datetime, timezone: str) -> str:
    return clock_module.to_local(at, timezone).strftime("%d.%m %H:%M")


def _counts_text(counts: Counts, *, preview: bool) -> str:
    verb = {
        "add": "добавить" if preview else "добавлено",
        "change": "изменить" if preview else "изменено",
        "remove": "убрать" if preview else "убрано",
        "tag": "дать id" if preview else "получили id",
        "create": "создать" if preview else "создано",
        "untag": "снять тег" if preview else "снят тег",
        "planner": "создать в планере" if preview else "создано в планере",
        "detach": "отцепить" if preview else "отцеплено",
    }
    parts = [
        f"карточки: {verb['add']} {counts.cards_added}, {verb['change']} {counts.cards_changed}, "
        f"{verb['remove']} {counts.cards_removed}, {verb['tag']} {counts.cards_tagged}",
        f"задачи: {verb['create']} {counts.tasks_created}, {verb['change']} {counts.tasks_changed}, "
        f"{verb['untag']} {counts.tasks_untagged}",
        (
            f"подзадачи: {verb['add']} {counts.subs_added}, {verb['change']} {counts.subs_changed}, "
            f"{verb['remove']} {counts.subs_removed}, {verb['planner']} {counts.subs_created}, "
            f"{verb['detach']} {counts.subs_detached}"
        ),
        f"связи: {verb['change']} {counts.deps_changed}",
        f"пар по названию {counts.paired}",
        f"конфликтов {counts.conflicts} (побеждает планер)",
        f"пропущено {counts.skipped}",
    ]
    if counts.waiting:
        parts.append(f"ждут следующего прохода {counts.waiting}")
    if counts.held:
        parts.append(
            f"придержано удалений {counts.held}: одна из сторон выглядит почти пустой. "
            "Если так и задумано, запусти /boards now"
        )
    return "; ".join(parts)


def result_text(result: board_job.PassResult) -> str:
    """The reply to `/boards preview` or `/boards now`, sent by the worker."""
    preview = result.trigger == board_job.TRIGGER_PREVIEW
    if result.outcome in RESULT_GATES:
        return RESULT_GATES[result.outcome]
    title = "Пробный проход (ничего не записано)" if preview else "Проход"
    lines = [f"{title}:"]
    for board in result.boards:
        if board.outcome == board_job.OK:
            lines.append(f"«{board.name}»: {_counts_text(board.counts, preview=preview)}.")
        else:
            lines.append(f"«{board.name}»: {OUTCOMES.get(board.outcome, board.outcome)}.")
    if result.outcome != board_job.OK:
        lines.append(f"Проход остановлен: {OUTCOMES.get(result.outcome, result.outcome)}.")
    return "\n".join(lines)


async def status_text(session, settings: Settings) -> str:
    """Bare `/boards`: stored state only."""
    state = await get_state(session)
    lines = [HEADER]
    if not settings.PLANNER_ENABLED:
        lines.append(PLANNER_OFF)
    elif not await planner_auth.is_enabled(session):
        lines.append(NOT_LINKED)
    if settings.VAULT_MODE == "off":
        lines.append(VAULT_OFF)
    lines.append(FLAG_ON if settings.BOARD_SYNC_ENABLED else FLAG_OFF)
    boards, whole = await board_job.status(session, settings)
    newest_board_run = max((b.last.at for b in boards if b.last is not None), default=None)
    if whole is not None and (newest_board_run is None or whole.at >= newest_board_run):
        if whole.outcome == board_job.NO_BOARDS:
            lines.append(NO_BOARDS)
        else:
            lines.append(
                f"Последний проход {_when(whole.at, state.timezone)}: {OUTCOMES.get(whole.outcome, whole.outcome)}."
            )
    for board in boards:
        line = f"«{board.name}» ⇄ #{board.tag}: пар {board.paired}"
        if board.held:
            line += f" (ждут правки {board.held})"
        if board.last is None:
            line += f"; {NEVER_RAN}."
        else:
            last = board.last
            line += f"; проход {_when(last.at, state.timezone)} — {OUTCOMES.get(last.outcome, last.outcome)}"
            if last.outcome == board_job.OK:
                line += f", конфликтов {last.conflicts}, пропущено {last.skipped}"
            line += "."
        lines.append(line)
    return "\n".join(lines)


async def command(sessionmaker, settings: Settings, clock: Clock, args: str | None, *, client_factory) -> str:
    """`/boards [preview|now|undo]`."""
    words = (args or "").split()
    async with sessionmaker() as session:
        if not words:
            return await status_text(session, settings)
        if len(words) != 1 or words[0] not in ("preview", "now", "undo"):
            return USAGE
        word = words[0]
        if settings.VAULT_MODE == "off":
            return VAULT_OFF
        if word == "undo":
            outcome, names = await echo_write.undo_boards(client_factory(settings))
            if outcome == echo_write.UNDONE:
                await board_job.forget_boards(session, names)
            return UNDO_REPLIES.get(outcome, UNDO_REPLIES[echo_write.UNAVAILABLE])
        if not settings.PLANNER_ENABLED:
            return PLANNER_OFF
        if not await planner_auth.is_enabled(session):
            return NOT_LINKED
        if word == "now" and not settings.BOARD_SYNC_ENABLED:
            return FLAG_OFF
        trigger = board_job.TRIGGER_NOW if word == "now" else board_job.TRIGGER_PREVIEW
        if not await board_job.enqueue_command(session, clock, trigger):
            return ALREADY_QUEUED
        return NOW_QUEUED if word == "now" else PREVIEW_QUEUED


async def digest_line(session, start: datetime.datetime, end: datetime.datetime) -> str | None:
    """The daily digest's board line for [start, end), or None when no
    pass in it met a conflict or a skipped title."""
    conflicts, skipped = await board_job.issues_between(session, start, end)
    if not conflicts and not skipped:
        return None
    return DIGEST_LINE.format(conflicts=conflicts, skipped=skipped)
