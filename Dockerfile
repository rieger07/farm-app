FROM python:3.11-alpine

WORKDIR /app

RUN (apk upgrade --no-cache || apk upgrade --no-cache) \
    && (apk upgrade --no-cache --repository=https://dl-cdn.alpinux.org/alpine/edge/main musl musl-utils sqlite-libs \
        || apk upgrade --no-cache --repository=https://dl-cdn.alpinux.org/alpine/edge/main musl musl-utils sqlite-libs || true) \
    && rm -rf /usr/local/lib/python*/ensurepip

COPY requirements.txt ./
RUN pip3 install --no-cache-dir --upgrade pip setuptools wheel \
    && pip3 install --no-cache-dir -r requirements.txt \
    && pip3 uninstall -y pip setuptools wheel \
    && addgroup -S appgroup && adduser -S appuser -G appgroup \
    && mkdir -p /app/data \
    && chown -R appuser:appgroup /app

COPY --chown=appuser:appgroup . .

ENV FARM_DB_PATH=/app/data/homestead.db
VOLUME ["/app/data"]

USER appuser

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8501/healthz || exit 1

ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8501", "--proxy-headers", "--forwarded-allow-ips=*"]
