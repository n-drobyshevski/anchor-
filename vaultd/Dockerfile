# The vault service (phase-8 plan section 5): obsidian-headless + vaultd.
#
# Build context is vaultd/ -- the Railway service's Root Directory is
# /vaultd (docs/vault-setup.md). Node 22 for ob, Python 3.12 via uv for
# vaultd, both pinned by their lockfiles.
FROM node:22-bookworm-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# obsidian-headless is UNLICENSED: installed from npm at build time,
# never vendored into this repository.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

COPY pyproject.toml uv.lock .python-version ./
RUN uv python install 3.12 && uv sync --frozen --no-dev --no-install-project

COPY vaultd ./vaultd
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "vaultd"]
