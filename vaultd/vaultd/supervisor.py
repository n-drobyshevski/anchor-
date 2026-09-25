"""Keeping `ob sync --continuous` alive (plan section 5.3).

When the child exits it is restarted with exponential backoff, 5 s
doubling to 5 min, and the backoff resets once a run has lasted
STABLE_RUN_S. `restarts`, `last_exit_code` and `running_since` are
exposed through `/v1/status`; the bot's deletion warmup (8c) reads
`running_since`, so it is None whenever the child is not running.

**The child's output never reaches a log.** Its stdout and stderr go
straight to /dev/null -- not drained through a pipe and dropped, but
never read at all, which is the stronger form of the plan's "drained
and discarded". They can carry file names, and a note's title is
content. What *is* logged: that the child started or exited, its exit
code, the restart count and the backoff.

**`ob` also tees its console into a file** of its own,
`$XDG_CONFIG_HOME/obsidian-headless/sync/<vault id>/sync.log`,
append-only and never rotated. It lives on the volume, so it is not a
log leak, but it grows without bound and holds the same file names.
Each start truncates it first, without reading it
(docs/decisions.md).

**The child's environment is an allowlist** built by boot.py: PATH,
HOME, XDG_CONFIG_HOME and OBSIDIAN_AUTH_TOKEN. It never sees
VAULT_API_TOKEN, and `ob sync` never sees the end-to-end password --
`sync-setup` stored the derived key, which is all it needs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from vaultd.config import BACKOFF_MAX_S, BACKOFF_MIN_S, STABLE_RUN_S

logger = logging.getLogger("vaultd.supervisor")

Sleep = Callable[[float], Awaitable[None]]


def truncate_sync_logs(config_home: Path) -> int:
    """Empty every ob sync.log under `config_home`, without reading any.

    Returns how many were truncated. Skips anything that is not a
    regular file, so a symlink planted there cannot redirect the
    truncation.
    """
    sync_dir = config_home / "obsidian-headless" / "sync"
    count = 0
    try:
        vault_dirs = list(os.scandir(sync_dir))
    except OSError:
        return 0
    for entry in vault_dirs:
        if not entry.is_dir(follow_symlinks=False):
            continue
        log_path = os.path.join(entry.path, "sync.log")
        try:
            st = os.lstat(log_path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
            continue
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            os.ftruncate(fd, 0)
            count += 1
        finally:
            os.close(fd)
    return count


class Supervisor:
    def __init__(
        self,
        argv: list[str],
        env: Mapping[str, str],
        config_home: Path,
        *,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        backoff_min: float = BACKOFF_MIN_S,
        backoff_max: float = BACKOFF_MAX_S,
        stable_after: float = STABLE_RUN_S,
    ) -> None:
        self.argv = list(argv)
        self.env = dict(env)
        self.config_home = config_home
        self._sleep = sleep
        self._monotonic = monotonic
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self.stable_after = stable_after
        self.restarts = 0
        self.last_exit_code: int | None = None
        self.running_since: datetime | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False

    def snapshot(self) -> dict:
        return {
            "sync_running": self.running_since is not None,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "running_since": self.running_since.isoformat() if self.running_since else None,
        }

    async def run(self) -> None:
        backoff = self.backoff_min
        while not self._stopping:
            truncate_sync_logs(self.config_home)
            started = self._monotonic()
            code = await self._run_once()
            if self._stopping:
                return
            if self._monotonic() - started >= self.stable_after:
                backoff = self.backoff_min
            logger.warning(
                "ob sync exited",
                extra={"exit_code": code, "restarts": self.restarts, "backoff_s": backoff},
            )
            await self._sleep(backoff)
            backoff = min(backoff * 2, self.backoff_max)
            self.restarts += 1

    async def _run_once(self) -> int | None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.env,
                start_new_session=True,
            )
        except OSError:
            # The binary is missing or not executable. Same handling as
            # a crash: back off and try again; /v1/status shows it.
            self.last_exit_code = -1
            logger.error("ob sync failed to start", extra={"event": "spawn_failed"})
            return -1
        self.running_since = datetime.now(timezone.utc)
        logger.info("ob sync started", extra={"restarts": self.restarts})
        try:
            code = await self._proc.wait()
        finally:
            self.running_since = None
        self.last_exit_code = code
        self._proc = None
        return code

    async def stop(self, timeout: float = 10.0) -> None:
        self._stopping = True
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
