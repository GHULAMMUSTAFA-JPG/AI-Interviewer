# ══════════════════════════════════════════════════════════════
# Meeting-Bot — Docker Image
#
# Joins Google Meet, scrapes captions, writes to interviews.transcripts.
#
# Build: docker build -t meeting-bot:latest .
# Run:   docker run --env-file .env meeting-bot:latest
# ══════════════════════════════════════════════════════════════

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN apt-get update && apt-get upgrade -y

RUN apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    build-essential \
    wget \
    curl \
    git

RUN apt-get install -y \
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release

RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - && \
    echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" | \
    tee /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y google-chrome-stable

RUN apt-get install -y \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    pulseaudio \
    pulseaudio-utils \
    alsa-utils \
    ffmpeg

RUN apt-get install -y \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libxshmfence1 \
    libx11-xcb1 \
    libxcb-dri3-0 \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-unifont \
    libappindicator3-1

WORKDIR /app

COPY . .

ENV PATH="/app/venv/bin:$PATH"

RUN pip install --upgrade pip && \
    pip install -r requirements.txt --no-cache-dir

RUN playwright install --with-deps chromium

RUN mkdir -p /app/recordings && chmod 777 /app/recordings

RUN mkdir -p /root/.config/pulse /var/run/pulse && \
    echo "default-sample-rate = 48000" >> /etc/pulse/daemon.conf && \
    echo "exit-idle-time = -1" >> /etc/pulse/daemon.conf && \
    # Allow any user/process to connect to PulseAudio in system mode
    sed -i 's/load-module module-native-protocol-unix$/load-module module-native-protocol-unix auth-anonymous=1/' \
        /etc/pulse/system.pa

ENV DISPLAY=:99
ENV PYTHONUNBUFFERED=1
ENV PULSE_SERVER=unix:/var/run/pulse/native

VOLUME ["/app/recordings"]

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
