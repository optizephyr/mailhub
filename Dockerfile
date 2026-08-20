# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY mailhub ./mailhub
COPY config.yaml ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 可编辑安装：config.yaml / data 相对 /app（mailhub/runtime/config.py 的仓库根）
RUN mkdir -p /app/data

CMD ["mailhub", "sync"]
