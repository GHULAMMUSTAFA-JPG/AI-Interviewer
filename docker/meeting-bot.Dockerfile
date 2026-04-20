# ════════════════════════════════════════════════════════════════════════════════
# Meeting-Bot — Headless Chrome + PulseAudio + Persistent Profile
# ════════════════════════════════════════════════════════════════════════════════

FROM ubuntu:22.04

# ════════════════════════════════════════════════════════════════════════════════
# Base Configuration
# ════════════════════════════════════════════════════════════════════════════════

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Set timezone — use azure.archive.ubuntu.com (reliable CDN, no 400 errors)
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80retries && \
    sed -i 's|http://archive.ubuntu.com/ubuntu|http://azure.archive.ubuntu.com/ubuntu|g' /etc/apt/sources.list && \
    sed -i 's|http://security.ubuntu.com/ubuntu|http://azure.archive.ubuntu.com/ubuntu|g' /etc/apt/sources.list

# ════════════════════════════════════════════════════════════════════════════════
# Core Dependencies + deadsnakes PPA (no software-properties-common needed)
# ════════════════════════════════════════════════════════════════════════════════

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Build tools
    wget \
    curl \
    gpg \
    gpg-agent \
    \
    # Certificate management
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release \
    \
    # Cleanup
    && rm -rf /var/lib/apt/lists/*

# Add deadsnakes PPA manually — avoids software-properties-common and its 100+ dependencies
RUN gpg --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys F23C5A6CF475977595C89F51BA6932366A755776 && \
    gpg --export F23C5A6CF475977595C89F51BA6932366A755776 | tee /etc/apt/trusted.gpg.d/deadsnakes.gpg > /dev/null && \
    echo "deb https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu jammy main" > /etc/apt/sources.list.d/deadsnakes.list

# Install Python 3.11
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# ════════════════════════════════════════════════════════════════════════════════
# Google Chrome Installation
# ════════════════════════════════════════════════════════════════════════════════

RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - && \
    echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" | \
    tee /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# Verify Chrome installation
RUN google-chrome --version

# ════════════════════════════════════════════════════════════════════════════════
# Xvfb — Virtual display (required for headless Chrome)
# ════════════════════════════════════════════════════════════════════════════════

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11-utils \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# ════════════════════════════════════════════════════════════════════════════════
# PulseAudio — Virtual Audio Sink for TTS
# ════════════════════════════════════════════════════════════════════════════════

RUN apt-get update --fix-missing || true && apt-get install -y --no-install-recommends --fix-missing \
    pulseaudio \
    pulseaudio-utils \
    alsa-utils \
    mpg123 \
    && rm -rf /var/lib/apt/lists/*

# Configure PulseAudio for system mode (required for root in Docker)
RUN mkdir -p /root/.config/pulse /var/run/pulse && \
    echo "default-sample-rate = 48000" >> /etc/pulse/daemon.conf && \
    echo "alternate-sample-rate = 44100" >> /etc/pulse/daemon.conf && \
    echo "exit-idle-time = -1" >> /etc/pulse/daemon.conf && \
    # Create default.pa for non-system mode (used by entrypoint.sh)
    echo 'load-module module-native-protocol-unix auth-anonymous=1 socket=/var/run/pulse/native' > /etc/pulse/default.pa && \
    echo 'load-module module-null-sink sink_name=virtual_mic sink_properties=device.description=VirtualMic' >> /etc/pulse/default.pa && \
    echo 'load-module module-remap-source source_name=virtual_mic_source master=virtual_mic.monitor source_properties=device.description=VirtualMicSource' >> /etc/pulse/default.pa && \
    echo 'set-default-sink virtual_mic' >> /etc/pulse/default.pa && \
    echo 'set-default-source virtual_mic_source' >> /etc/pulse/default.pa && \
    # Also fix system.pa for backwards compatibility
    sed -i 's/load-module module-native-protocol-unix$/load-module module-native-protocol-unix auth-anonymous=1/' /etc/pulse/system.pa

# ════════════════════════════════════════════════════════════════════════════════
# Chrome Dependencies — Libraries required by Chromium
# ════════════════════════════════════════════════════════════════════════════════

RUN apt-get update && apt-get install -y --no-install-recommends \
    # NSS security libraries
    libnss3 \
    libnss3-dev \
    \
    # ATK accessibility
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    \
    # X11 libraries
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libxkbcommon0 \
    libx11-xcb1 \
    libxcb-dri3-0 \
    \
    # Graphics libraries
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libxshmfence1 \
    \
    # System libraries
    libappindicator3-1 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbfile1 \
    \
    # Cleanup
    && rm -rf /var/lib/apt/lists/*

# ════════════════════════════════════════════════════════════════════════════════
# Application Setup
# ════════════════════════════════════════════════════════════════════════════════

WORKDIR /app

# Copy application files
COPY . .

# Create Python virtual environment
RUN python3.11 -m venv /app/venv && \
    /app/venv/bin/pip install --upgrade pip

# Install Python dependencies
COPY requirements.txt .
RUN /app/venv/bin/pip install -r requirements.txt --no-cache-dir

# Install only Playwright's OS-level dependencies — no browser download.
# The bot uses channel="chrome" (system Google Chrome), so Playwright's
# own Chromium is never used and should not be downloaded.
RUN /app/venv/bin/playwright install-deps chromium

# ════════════════════════════════════════════════════════════════════════════════
# Directory Setup
# ════════════════════════════════════════════════════════════════════════════════

# Create recordings directory with proper permissions
RUN mkdir -p /app/recordings /app/chrome_profile && \
    chmod 777 /app/recordings /app/chrome_profile

# ════════════════════════════════════════════════════════════════════════════════
# Environment Variables
# ════════════════════════════════════════════════════════════════════════════════

ENV PATH="/app/venv/bin:$PATH"
ENV DISPLAY=:99
ENV PYTHONUNBUFFERED=1
ENV PULSE_SERVER=unix:/var/run/pulse/native

# ════════════════════════════════════════════════════════════════════════════════
# Volumes
# ════════════════════════════════════════════════════════════════════════════════

# Recordings: Transcript files and logs
VOLUME ["/app/recordings"]

# Chrome profile: Persistent browser data (cookies, login sessions)
VOLUME ["/app/chrome_profile"]

# Pulse socket: Shared with TTS container for audio routing
VOLUME ["/var/run/pulse"]

# ════════════════════════════════════════════════════════════════════════════════
# Entrypoint
# ════════════════════════════════════════════════════════════════════════════════

# Copy and configure entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD pgrep -f "python3 main.py" || exit 1
