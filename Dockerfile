# ---------- Build stage ----------
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install dependencies first (separate layer, cached while only app code changes).
# Deliberately no BuildKit `RUN --mount=...` cache/bind syntax here - Cloud Run's
# default "deploy from source" build pipeline runs the classic (non-BuildKit) docker
# builder, which rejects that syntax outright ("the --mount option requires BuildKit").
# This trades away persistent cross-build uv cache for guaranteed build-pipeline
# portability; Docker's normal layer cache still applies within a single build.
COPY uv.lock pyproject.toml /app/
RUN uv sync --frozen --no-install-project --no-dev

COPY . /app

RUN uv sync --frozen --no-dev

# ---------- Runtime stage ----------
FROM python:3.11-slim

# libgomp1 is required at runtime by torch/scikit-learn/hdbscan (OpenMP)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PORT=8000

USER appuser

EXPOSE 8000

# Cloud Run injects PORT (typically 8080) and requires the container to listen on it;
# shell form is required here so $PORT actually expands (defaults to 8000 for local `docker run`).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD sh -c "curl -f http://localhost:${PORT:-8000}/health || exit 1"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
