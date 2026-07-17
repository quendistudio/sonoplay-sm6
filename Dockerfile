FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies in builder stage
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source and install package
COPY src/ src/
RUN pip install --no-cache-dir .

# --- Final stage: slim runtime image ---
FROM python:3.12-slim

WORKDIR /app

# Create non-root user
RUN groupadd -r sonoplay && useradd -r -g sonoplay -d /app -s /sbin/nologin sonoplay

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY templates/ templates/
COPY static/ static/
COPY main.py logging_config.py version.py ./
COPY plex/ plex/
COPY dlna/ dlna/
COPY settings/ settings/
COPY utils/ utils/
COPY src/ src/

# ffmpeg: Plex HLS → progressive MP3 for SM6
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set ownership
RUN chown -R sonoplay:sonoplay /app

# Pre-create config directory with correct ownership so that
# atomic_write_json can create temp files in it.
# Must come BEFORE the VOLUME declaration so Docker preserves
# the ownership when creating an empty volume at runtime.
RUN mkdir -p /config && chown sonoplay:sonoplay /config

ENV HTTP_PORT=32488 CONFIG_PATH=/config
EXPOSE 1910/udp 32412/udp $HTTP_PORT
VOLUME /config

# Switch to non-root user
USER sonoplay

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${HTTP_PORT}/health')" || exit 1

CMD ["python", "-O", "main.py"]
