# Use a slim Alpine version of Python to keep the image size and attack surface small
FROM python:3.11-alpine

# Set the working directory inside the container
WORKDIR /app

# 1. Upgrade all Alpine OS packages (OpenSSL, musl, zlib, sqlite, xz, krb5, etc.) to patch OS CVEs
# 2. Remove bundled ensurepip wheels (pip-24.0 / setuptools) that scanners flag for known CVEs
# 3. Upgrade pip, setuptools, wheel, and install latest patched Python dependencies
# 4. Create a non-root user and persistent data directory
RUN (apk upgrade --no-cache || apk upgrade --no-cache) \
    && rm -rf /usr/local/lib/python*/ensurepip \
    && pip3 install --no-cache-dir --upgrade \
        pip \
        setuptools \
        wheel \
        streamlit \
        streamlit-vis-timeline \
    && addgroup -S appgroup && adduser -S appuser -G appgroup \
    && mkdir -p /app/data \
    && chown -R appuser:appgroup /app

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

# Command to run the app
ENTRYPOINT ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
