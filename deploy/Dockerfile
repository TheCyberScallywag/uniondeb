# syntax=docker/dockerfile:1
# Uniondeb images.
# Build context is the repository root (see deploy/compose.yml):
#   dev      the whole uv workspace with dev tools — pytest, ruff
#   runtime  the `uniondeb` CLI only, no dev tools, non-root

ARG UV_VERSION=0.12.19
ARG PYTHON_VERSION=3.14

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-trixie-slim AS base
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app


FROM base AS dev
# git: pre-commit and other tools read the bind-mounted checkout.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /app
# Third-party dependencies first, so they stay cached until uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    uv sync --locked --all-packages --no-install-workspace
COPY . .
# Workspace members are installed editable, so a bind mount over /app is picked up live.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-packages
CMD ["pytest"]


FROM base AS build
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    uv sync --locked --all-packages --extra relate --no-dev --no-install-workspace
COPY pyproject.toml uv.lock ./
COPY packages packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-packages --extra relate --no-dev --no-editable


FROM python:${PYTHON_VERSION}-slim-trixie AS runtime
RUN useradd --uid 10001 --create-home uniondeb \
    && install -d -o uniondeb -g uniondeb -m 0700 /var/lib/uniondeb
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER uniondeb
# Run directories and exported schemas are read and written here (bind-mounted by compose).
WORKDIR /work
ENTRYPOINT ["uniondeb"]
CMD ["--help"]
