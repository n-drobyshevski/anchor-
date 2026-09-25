"""The supervisor: restart with backoff, what it exposes, what it truncates (plan 5.3)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vaultd.supervisor import Supervisor, truncate_sync_logs


class Stop(Exception):
    pass


def _recording_sleep(delays: list[float], stop_after: int):
    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= stop_after:
            raise Stop
        await asyncio.sleep(0)

    return sleep


async def test_an_exiting_child_is_restarted_with_doubling_backoff(fake_ob, data_dir: Path) -> None:
    fake_ob.set("sync", exit=3)
    delays: list[float] = []
    sup = Supervisor(
        [str(fake_ob.bin), "sync", "--continuous", "--path", str(data_dir / "vault")],
        {"PATH": "/usr/bin:/bin"},
        data_dir / "config",
        sleep=_recording_sleep(delays, stop_after=8),
    )
    with pytest.raises(Stop):
        await sup.run()
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300]
    assert sup.restarts == 7
    assert sup.last_exit_code == 3
    assert sup.snapshot()["sync_running"] is False
    assert sup.snapshot()["running_since"] is None
    assert fake_ob.commands() == ["sync"] * 8


async def test_a_stable_run_resets_the_backoff(fake_ob, data_dir: Path) -> None:
    fake_ob.set("sync", exit=1)
    ticks = iter([0, 1, 10, 11, 20, 400, 500, 501, 600, 601])
    delays: list[float] = []
    sup = Supervisor(
        [str(fake_ob.bin), "sync"],
        {"PATH": "/usr/bin:/bin"},
        data_dir / "config",
        sleep=_recording_sleep(delays, stop_after=5),
        monotonic=lambda: next(ticks),
    )
    with pytest.raises(Stop):
        await sup.run()
    # Runs lasted 1, 1, 380, 1, 1 "seconds": the long one resets to the floor.
    assert delays == [5, 10, 5, 10, 20]


async def test_status_while_running(fake_ob, data_dir: Path) -> None:
    fake_ob.set("sync", sleep=30)
    sup = Supervisor([str(fake_ob.bin), "sync"], {"PATH": "/usr/bin:/bin"}, data_dir / "config")
    task = asyncio.create_task(sup.run())
    for _ in range(200):
        if sup.snapshot()["sync_running"]:
            break
        await asyncio.sleep(0.01)
    snap = sup.snapshot()
    assert snap["sync_running"] is True
    assert snap["running_since"] is not None
    await sup.stop()
    await asyncio.wait_for(task, 5)
    assert sup.snapshot()["sync_running"] is False


async def test_a_missing_binary_backs_off_instead_of_crashing(data_dir: Path) -> None:
    delays: list[float] = []
    sup = Supervisor(
        [str(data_dir / "no-such-ob"), "sync"],
        {"PATH": "/usr/bin:/bin"},
        data_dir / "config",
        sleep=_recording_sleep(delays, stop_after=2),
    )
    with pytest.raises(Stop):
        await sup.run()
    assert delays == [5, 10]
    assert sup.last_exit_code == -1


def test_sync_logs_are_truncated_without_being_read(data_dir: Path, tmp_path: Path) -> None:
    sync_dir = data_dir / "config" / "obsidian-headless" / "sync"
    (sync_dir / "vault-one").mkdir(parents=True)
    log = sync_dir / "vault-one" / "sync.log"
    log.write_text("[2026-09-25] Uploading Секретная заметка.md\n")
    elsewhere = tmp_path / "elsewhere.log"
    elsewhere.write_text("not ob's")
    (sync_dir / "vault-two").mkdir()
    (sync_dir / "vault-two" / "sync.log").symlink_to(elsewhere)

    assert truncate_sync_logs(data_dir / "config") == 1
    assert log.read_text() == ""
    assert elsewhere.read_text() == "not ob's"
    assert truncate_sync_logs(data_dir / "config") == 0
    assert truncate_sync_logs(tmp_path / "nothing-here") == 0


async def test_each_start_truncates_the_sync_log(fake_ob, data_dir: Path) -> None:
    log = data_dir / "config" / "obsidian-headless" / "sync" / "v" / "sync.log"
    log.parent.mkdir(parents=True)
    log.write_text("old names\n")
    fake_ob.set("sync", exit=1)
    delays: list[float] = []
    sup = Supervisor(
        [str(fake_ob.bin), "sync"], {"PATH": "/usr/bin:/bin"}, data_dir / "config",
        sleep=_recording_sleep(delays, stop_after=1),
    )
    with pytest.raises(Stop):
        await sup.run()
    assert log.read_text() == ""
