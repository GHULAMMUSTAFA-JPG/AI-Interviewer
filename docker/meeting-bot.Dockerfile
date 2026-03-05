# ════════════════════════════════════════════════════════════════════════════════
# Meeting-Bot — Docker Image with Robust VNC Support
# ════════════════════════════════════════════════════════════════════════════════
#
# This Docker image provides a headless Chrome browser with:
#   - Xvfb: Virtual display server (1280x720 resolution)
#   - x11vnc: VNC server for remote desktop access
#   - noVNC: Web-based VNC client (access via browser)
#   - PulseAudio: Virtual audio sink for TTS integration
#   - Google Chrome: Browser that joins Google Meet
#   - Playwright: Browser automation library
#
# Build:
#   docker build -t meeting-bot:latest .
#
# Run:
#   docker run --env-file .env \
#     -p 6080:6080 -p 5900:5900 \
#     -v meeting_bot_recordings:/app/recordings \
#     -v pulse-socket:/var/run/pulse \
#     --shm-size="2gb" \
#     meeting-bot:latest
#
# OR with Docker Compose:
#   docker compose up meeting-bot
#
# Access:
#   VNC (Browser):  http://localhost:6080/
#   VNC (Client):   localhost:5900
#
# ════════════════════════════════════════════════════════════════════════════════

FROM ubuntu:22.04

# ════════════════════════════════════════════════════════════════════════════════
# Base Configuration
# ════════════════════════════════════════════════════════════════════════════════

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Set timezone
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y software-properties-common && \
    add-apt-repository ppa:deadsnakes/ppa

# ════════════════════════════════════════════════════════════════════════════════
# Core Dependencies
# ════════════════════════════════════════════════════════════════════════════════

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    \
    # Build tools
    wget \
    curl \
    \
    # Certificate management
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release \
    \
    # Cleanup
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

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
# VNC Server Components (Xvfb, x11vnc, noVNC)
# ════════════════════════════════════════════════════════════════════════════════

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Xvfb - X Virtual Framebuffer (virtual display)
    xvfb \
    \
    # x11vnc - VNC server for X11
    x11vnc \
    \
    # websockify - WebSocket proxy for VNC
    websockify \
    \
    # X11 utilities
    x11-apps \
    x11-utils \
    xterm \
    \
    # Fonts for better rendering
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-unifont \
    \
    # Cleanup
    && rm -rf /var/lib/apt/lists/*

# Download and install noVNC v1.4.0 (stable release)
# This is baked into the image at build time for reliability
RUN wget -qO /tmp/novnc.tar.gz \
        https://github.com/novnc/noVNC/archive/refs/tags/v1.4.0.tar.gz && \
    tar -xzf /tmp/novnc.tar.gz -C /opt && \
    mv /opt/noVNC-1.4.0 /opt/novnc && \
    rm /tmp/novnc.tar.gz && \
    \
    # Landing page — no auto-redirect, user clicks Connect manually.
    # Meta-refresh causes the loading screen issue when no meeting is active.
    cat > /opt/novnc/index.html << 'NOVNC_INDEX'
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Meeting Bot — VNC Access</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%);
      color: #ffffff;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .container { text-align: center; max-width: 600px; }
    h1 { font-size: 2.5rem; margin-bottom: 10px; font-weight: 600; }
    .subtitle { font-size: 1.1rem; opacity: 0.8; margin-bottom: 40px; }
    .status-box {
      background: rgba(255, 255, 255, 0.1);
      border-radius: 12px;
      padding: 30px;
      margin-bottom: 30px;
    }
    .status-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 15px 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
    }
    .status-item:last-child { border-bottom: none; }
    .status-label { font-size: 0.95rem; opacity: 0.9; }
    .status-value {
      font-family: monospace;
      background: rgba(0, 0, 0, 0.3);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 0.9rem;
    }
    .connect-btn {
      display: inline-block;
      background: #4a9eff;
      color: #ffffff;
      text-decoration: none;
      padding: 16px 48px;
      border-radius: 8px;
      font-size: 1.1rem;
      font-weight: 600;
    }
    .connect-btn:hover { background: #2d7dd2; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Meeting Bot</h1>
    <p class="subtitle">Virtual Display Access</p>
    <div class="status-box">
      <div class="status-item">
        <span class="status-label">Display</span>
        <span class="status-value">:99 (1280x720)</span>
      </div>
      <div class="status-item">
        <span class="status-label">VNC Port</span>
        <span class="status-value">5900</span>
      </div>
      <div class="status-item">
        <span class="status-label">Web Access</span>
        <span class="status-value">6080</span>
      </div>
    </div>
    <a href="vnc.html?autoconnect=1&resize=scale&reconnect=1" class="connect-btn">Connect to Desktop</a>
  </div>
</body>
</html>
NOVNC_INDEX

# ════════════════════════════════════════════════════════════════════════════════
# PulseAudio — Virtual Audio Sink for TTS
# ════════════════════════════════════════════════════════════════════════════════

RUN apt-get update && apt-get install -y --no-install-recommends \
    # PulseAudio sound server
    pulseaudio \
    pulseaudio-utils \
    \
    # ALSA utilities (fallback audio)
    alsa-utils \
    \
    # Cleanup
    && rm -rf /var/lib/apt/lists/*

# Configure PulseAudio for system mode (required for root in Docker)
RUN mkdir -p /root/.config/pulse /var/run/pulse && \
    echo "default-sample-rate = 48000" >> /etc/pulse/daemon.conf && \
    echo "alternate-sample-rate = 44100" >> /etc/pulse/daemon.conf && \
    echo "exit-idle-time = -1" >> /etc/pulse/daemon.conf && \
    # Replace the existing unix socket line to add auth-anonymous=1.
    # Appending would create a duplicate entry; the original line loads first
    # without auth-anonymous and blocks anonymous connections from TTS/pacat.
    sed -i 's/load-module module-native-protocol-unix$/load-module module-native-protocol-unix auth-anonymous=1/' \
        /etc/pulse/system.pa

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

# Copy health check script
COPY vnc_healthcheck.sh /app/vnc_healthcheck.sh
RUN chmod +x /app/vnc_healthcheck.sh

# Set entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]

# ════════════════════════════════════════════════════════════════════════════════
# Health Check
# ════════════════════════════════════════════════════════════════════════════════

# Check that the main Python process is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD pgrep -f "python3 main.py" || exit 1

# ════════════════════════════════════════════════════════════════════════════════
# Metadata
# ════════════════════════════════════════════════════════════════════════════════

LABEL maintainer="AI Interviewer Project"
LABEL description="Meeting Bot with VNC access for Google Meet automation"
LABEL version="2.0"
