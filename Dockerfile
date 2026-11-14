# Use a slim Alpine version of Python to keep the image size and attack surface small
FROM python:3.11-alpine

# Set the working directory inside the container
WORKDIR /app

# 1. Upgrade all Alpine OS packages and pull patched musl (1.2.6-r6) & sqlite-libs (3.54.1-r0) from edge
# 2. Remove bundled ensurepip wheels
# 3. Install latest patched application dependencies
# 4. Uninstall pip, setuptools, and wheel (removes build tools and pip's vendored setuptools@70.3.0 SBOM)
# 5. Create a non-root user and persistent data directory
RUN (apk upgrade --no-cache || apk upgrade --no-cache) \
    && (apk upgrade --no-cache --repository=https://dl-cdn.alpinelinux.org/alpine/edge/main musl musl-utils sqlite-libs \
        || apk upgrade --no-cache --repository=https://dl-cdn.alpinelinux.org/alpine/edge/main musl musl-utils sqlite-libs) \
    && rm -rf /usr/local/lib/python*/ensurepip \
    && pip3 install --no-cache-dir --upgrade \
        pip \
        setuptools \
        wheel \
        streamlit \
        streamlit-vis-timeline \
    && pip3 uninstall -y pip setuptools wheel \
    && addgroup -S appgroup && adduser -S appuser -G appgroup \
    && mkdir -p /app/data \
    && chown -R appuser:appgroup /app /usr/local/lib/python*/site-packages/streamlit_timeline

# Copy application code into the container with non-root ownership
COPY --chown=appuser:appgroup . .

# Configure persistent SQLite database path
ENV FARM_DB_PATH=/app/data/homestead.db
VOLUME ["/app/data"]

# Run as non-root user
USER appuser

# Expose the default Streamlit port
EXPOSE 8501

# Configure healthcheck using built-in BusyBox wget (avoids installing curl)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8501/_stcore/health || exit 1

# Command to run the app (configured for reverse-proxy deployments behind Nginx / Cloudflare)
ENTRYPOINT [ \
    "streamlit", "run", "app.py", \
    "--server.port=8501", \
    "--server.address=0.0.0.0", \
    "--server.headless=true", \
    "--browser.gatherUsageStats=false", \
    "--server.enableCORS=false", \
    "--server.enableXsrfProtection=false" \
]
