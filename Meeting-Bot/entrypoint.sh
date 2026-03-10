#!/bin/bash
set -e

export DISPLAY=:99
export PULSE_SERVER=unix:/var/run/pulse/native
export PYTHONUNBUFFERED=1

RECORDINGS_DIR="/app/recordings"
mkdir -p "$RECORDINGS_DIR"

log() { echo "[$(date '+%H:%M:%S')] $1"; }

# Clean stale locks
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
pkill -9 Xvfb pulseaudio 2>/dev/null || true
rm -f /var/run/pulse/native /var/run/pulse/pid 2>/dev/null || true
rm -rf /var/run/pulse/.config 2>/dev/null || true
chown -R pulse:pulse /var/run/pulse 2>/dev/null || true
chmod 755 /var/run/pulse
[ -d /app/chrome_profile ] && rm -f /app/chrome_profile/Singleton{Lock,Cookie,Socket} 2>/dev/null || true

# Xvfb
log "Starting Xvfb :99 (1280x720x24)..."
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset -nolisten tcp &
sleep 2
pgrep -x Xvfb > /dev/null && log "Xvfb running" || { log "Xvfb failed"; exit 1; }

# PulseAudio
log "Starting PulseAudio..."
mkdir -p /var/run/pulse /root/.config/pulse
pulseaudio --system --daemonize=yes --exit-idle-time=-1 --disallow-exit 2>/dev/null || true
sleep 2

if pactl info &>/dev/null; then
    log "PulseAudio running"
    pactl load-module module-null-sink sink_name=virtual_mic sink_properties=device.description=VirtualMic 2>/dev/null && log "virtual_mic sink created" || true
    pactl load-module module-virtual-source source_name=virtual_mic_source master=virtual_mic.monitor 2>/dev/null && log "virtual_mic_source created" || true
    pactl set-default-sink virtual_mic 2>/dev/null || true
    pactl set-default-source virtual_mic_source 2>/dev/null || true
else
    log "PulseAudio failed to start - TTS will handle gracefully"
fi

log "Environment ready - starting Meeting Bot..."
exec python3 main.py
