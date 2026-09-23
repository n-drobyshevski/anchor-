# Milestone 6e (Phase 6 plan section 9.1 and the approved decision on
# pg_dump 18). Railway's own buildpack has no way to add a system
# package (the postgres-client matching the server's major version,
# for app/ops/backup.py's nightly pg_dump), so the service moves to
# this Dockerfile as its builder (railway.json). Nothing about the
# deploy's start command or healthcheck changes -- those stay
# configured on the Railway service itself, exactly as
# railway.json's own comment says.
FROM python:3.12-slim

# PGDG's own apt repo, for postgresql-client-18 -- Debian's default
# repos only carry whatever major version shipped with the base image,
# which is not guaranteed to match Railway's postgres-ssl:18 server.
# pg_dump's client major version must be >= the server's.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg lsb-release \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
        https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && apt-get purge -y --auto-remove curl gnupg lsb-release \
    && rm -rf /var/lib/apt/lists/*

# app/config.py's BACKUP_PG_DUMP defaults to "pg_dump", found on PATH --
# postgresql-client-18 installs it at /usr/lib/postgresql/18/bin, which
# is not on PATH by default on Debian.
ENV PATH="/usr/lib/postgresql/18/bin:${PATH}"

# uv, for `uv sync`/`uv run` -- the same tool this repo already uses
# for local development (pyproject.toml, uv.lock).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependencies first, for layer caching: this layer only invalidates
# when pyproject.toml/uv.lock change, not on every source edit.
# --no-install-project: the project itself (hatchling, packages=["app"])
# needs its source, which is not copied yet.
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The rest of the app. .dockerignore keeps .venv, .git, test caches and
# eval/reports out of the build context.
COPY . .
RUN uv sync --frozen --no-dev

# The Railway start command is `uv run ...`. Without UV_NO_SYNC, uv run
# would re-sync at every start and pull the dev group (pytest,
# pip-audit) over the network. The image is already synced; run as is.
ENV PYTHONUNBUFFERED=1 UV_NO_SYNC=1

# The same command railway.json sets as the Railway start command:
# migrate (scripts/migrate.py exits via os._exit, see its docstring),
# then start the app -- the venv's python directly, no `uv run`.
CMD ["sh", "-c", "/app/.venv/bin/python scripts/migrate.py && /app/.venv/bin/python -m app.main"]
