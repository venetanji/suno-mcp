FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV SUNO_DOWNLOAD_DIR=/downloads
ENV SUNO_AUTH_DIR=/data

# System dependencies: Xvfb, x11vnc, noVNC, supervisor
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    supervisor \
    python3.11 \
    python3.11-venv \
    python3-pip \
    curl \
    ca-certificates \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libcups2 \
    libxss1 \
    libgtk-3-0 \
    libx11-xcb1 \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python package management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set up the project
WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/

# Create venv and install dependencies
RUN uv venv /app/.venv --python python3.11 \
    && . /app/.venv/bin/activate \
    && uv pip install . \
    && playwright install chromium --with-deps

# Create volume directories
RUN mkdir -p /downloads /data

# Supervisord config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 6080

ENTRYPOINT ["/entrypoint.sh"]
