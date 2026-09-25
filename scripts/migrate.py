"""Run `alembic upgrade head`, then exit immediately.

The Railway start command is `migrate && app`. In the 6e Docker image,
`uv run alembic upgrade head` finished its work (env.py printed its
"migrations done" marker) but the process never exited, so the app
never started and every deploy timed out on the healthcheck. This
script runs the same upgrade in-process and then leaves via os._exit,
which skips interpreter finalization entirely -- nothing left over from
the migration can hold the process open. A failed migration still
raises and exits non-zero, so `&&` still stops the app from starting on
a bad schema.
"""

from __future__ import annotations

import os
import pathlib
import sys

from alembic import command
from alembic.config import Config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> None:
    print("migrate: upgrading to head", file=sys.stderr, flush=True)
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
    print("migrate: done", file=sys.stderr, flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
