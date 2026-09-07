# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Build stage: install dependencies into a self-contained virtualenv so the
# runtime image carries no compiler toolchain or pip cache.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="issues-gateway" \
      org.opencontainers.image.description="HTTP gateway over the GitHub Issues REST API" \
      org.opencontainers.image.source="https://github.com/mikechau1/CMPE272-HW2"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    LOG_FORMAT=json \
    EVENT_STORE_PATH=/data/events.db

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY app ./app
COPY openapi.yaml ./openapi.yaml

# Run unprivileged. /data is a volume mount point for the SQLite event store,
# which must outlive the container for webhook dedupe to survive a restart.
RUN useradd --create-home --uid 10001 gateway \
    && mkdir -p /data \
    && chown -R gateway:gateway /app /data
USER gateway
VOLUME ["/data"]

EXPOSE 8000

# Liveness only -- deliberately does not reach GitHub, so an upstream outage
# or an exhausted rate limit cannot make the orchestrator kill a healthy pod.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4).status==200 else 1)"

# Shell form so ${PORT} is expanded at runtime; exec so uvicorn is PID 1 and
# receives SIGTERM directly for a clean shutdown.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
