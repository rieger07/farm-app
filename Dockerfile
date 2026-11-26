# Homestead Planner (port 8501) on the latest stable Debian-slim Python
FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./

# 1. Apply Debian security updates (tzdata ships with the python slim image)
# 2. Install application dependencies from requirements.txt
# 3. Uninstall pip (removes build tooling from the runtime image)
# 4. Create a non-root user and persistent data directory
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip setuptools wheel \
    && groupadd --system appgroup \
    && useradd --system --gid appgroup --no-create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appgroup /app

COPY --chown=appuser:appgroup . .

ENV FARM_DB_PATH=/app/data/homestead.db
VOLUME ["/app/data"]

USER appuser

EXPOSE 8501

# Slim images have no wget/curl, so probe /healthz with the Python stdlib
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/healthz', timeout=4)" || exit 1

ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8501", "--proxy-headers", "--forwarded-allow-ips=*"]
