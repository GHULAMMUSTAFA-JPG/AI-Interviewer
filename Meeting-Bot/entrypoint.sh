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
    # TTS path: TTS writes to virtual_mic → virtual_mic_source → Meet WebRTC → candidate hears bot
    pactl load-module module-null-sink sink_name=virtual_mic sink_properties=device.description=VirtualMic 2>/dev/null && log "virtual_mic sink created" || true
    pactl load-module module-virtual-source source_name=virtual_mic_source master=virtual_mic.monitor 2>/dev/null && log "virtual_mic_source created" || true
    
    # STT path: Meet audio output → Chrome speakers → BotMic (for Web Speech API)
    # Create BotMic as a monitor of virtual_mic_source (captures what Chrome hears from Meet)
    pactl load-module module-null-sink sink_name=BotMic channels=1 rate=16000 2>/dev/null && log "BotMic sink created (for STT)" || true
    
    # Route: virtual_mic_source.monitor (Chrome output) → BotMic
    # This captures meeting audio (candidate's voice) for SpeechRecognition
    pactl load-module module-loopback source=virtual_mic_source.monitor sink=BotMic latency_msec=10 2>/dev/null && log "BotMic routing active (candidate voice → STT)" || true
    
    # Defaults: Chrome uses virtual_mic for output, STT uses BotMic
    pactl set-default-sink virtual_mic 2>/dev/null || true
    pactl set-default-source virtual_mic_source 2>/dev/null || true
else
    log "PulseAudio failed to start - TTS will handle gracefully"
fi

log "Environment ready - starting Meeting Bot..."
exec python3 main.py
