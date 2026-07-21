# Buzz Lightyear — pairing server image (Flask webapp only).
# Controller voice deps (vosk / sounddevice) are intentionally omitted.
#
# Build from repo root:
#   docker build -f deploy/docker/Dockerfile -t buzz-pairing .
#
# Or via compose:
#   docker compose -f deploy/caddy/compose.yml up --build

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app

# Install dependencies into a project venv (no controller group, no project package).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-default-groups --no-install-project

# Application sources needed by the pairing helper.
COPY config.py pairing.py actions.py triggers.py ./
COPY webapp/ ./webapp/


FROM python:3.13-slim-bookworm AS runtime

# Non-root user
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app

# Copy venv + app from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/config.py /app/pairing.py /app/actions.py /app/triggers.py ./
COPY --from=builder /app/webapp ./webapp

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    WEB_DEBUG=0 \
    PAIRING_STATE_PATH=/data/pairing_state.json \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=8 \
    GUNICORN_TIMEOUT=60

# Persistent pair result (uid / paired status) across container restarts
RUN mkdir -p /data && chown -R appuser:appuser /data /app
VOLUME ["/data"]

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"

# Single worker: pairing sessions are process-local (pairing.manager).
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--capture-output", \
     "webapp.wsgi:app"]
