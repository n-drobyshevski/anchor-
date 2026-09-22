#!/usr/bin/env bash
# Provision the PostgreSQL version production runs (18), for the test suite.
#
# Production is Railway's ghcr.io/railwayapp-templates/postgres-ssl:18.
# tests/conftest.py prefers an 18 cluster and warns on anything older.
#
# Idempotent: safe to re-run. Ubuntu/Debian only; on anything else,
# install PostgreSQL 18 however that platform does it and point
# ANCHOR_ADMIN_DATABASE_URL at it.
set -euo pipefail

PG_MAJOR="${PG_MAJOR:-18}"
PG_PORT="${PG_PORT:-5433}"
PG_CLUSTER="${PG_CLUSTER:-test}"
PG_USER="${PG_USER:-anchor}"
PG_PASSWORD="${PG_PASSWORD:-anchor}"

if [ "$(id -u)" -ne 0 ]; then
  echo "This script installs system packages; re-run it with sudo." >&2
  exit 1
fi

if [ ! -d "/usr/lib/postgresql/${PG_MAJOR}" ]; then
  echo "==> Adding the PGDG apt repository"
  . /etc/os-release
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  echo "==> Installing PostgreSQL ${PG_MAJOR}"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    "postgresql-${PG_MAJOR}" "postgresql-client-${PG_MAJOR}"
fi

if ! pg_lsclusters -h | awk '{print $1" "$2}' | grep -qx "${PG_MAJOR} ${PG_CLUSTER}"; then
  echo "==> Creating cluster ${PG_MAJOR}/${PG_CLUSTER} on port ${PG_PORT}"
  # C.UTF-8, not plain C: under a plain C locale pg_trgm silently stops
  # seeing Cyrillic (zero trigrams, zero similarity, no error), which
  # would make memory retrieval return nothing and look like a bug in
  # the retrieval code. See tests/conftest.py.
  pg_createcluster "${PG_MAJOR}" "${PG_CLUSTER}" -p "${PG_PORT}" -- \
    --locale=C.UTF-8 --encoding=UTF8
fi

pg_ctlcluster "${PG_MAJOR}" "${PG_CLUSTER}" start || true

echo "==> Ensuring role ${PG_USER}"
su postgres -c "psql -p ${PG_PORT} -tAc \"SELECT 1 FROM pg_roles WHERE rolname='${PG_USER}'\"" \
  | grep -q 1 || su postgres -c \
  "psql -p ${PG_PORT} -tAc \"CREATE ROLE ${PG_USER} LOGIN SUPERUSER PASSWORD '${PG_PASSWORD}'\""

cat <<MSG

Done. Point the test suite at this cluster:

  export ANCHOR_ADMIN_DATABASE_URL="postgresql://${PG_USER}:${PG_PASSWORD}@127.0.0.1:${PG_PORT}/postgres"
  uv run pytest

MSG
