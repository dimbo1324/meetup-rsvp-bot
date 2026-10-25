# Многоэтапная сборка: зависимости ставятся в builder, в runtime копируется только .venv и код.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.14-slim AS runtime
# Версия для сообщения «Бот запущен»: APP_VERSION=$(git rev-parse --short HEAD) docker compose build
ARG APP_VERSION=
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 APP_VERSION=${APP_VERSION}
WORKDIR /app
RUN useradd --create-home --uid 10001 bot && mkdir -p /app/data && chown bot:bot /app/data
COPY --from=builder --chown=bot:bot /app/.venv ./.venv
COPY --chown=bot:bot src ./src
COPY --chown=bot:bot alembic ./alembic
COPY --chown=bot:bot alembic.ini ./
COPY --chown=bot:bot --chmod=755 docker/entrypoint.sh ./entrypoint.sh
USER bot
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,sys,time; p='/app/data/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<180 else 1)"
CMD ["./entrypoint.sh"]
