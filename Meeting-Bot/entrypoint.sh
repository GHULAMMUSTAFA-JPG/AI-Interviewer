#!/bin/bash
set -e

export PYTHONUNBUFFERED=1

log() { echo "[$(date '+%H:%M:%S')] $1"; }

# ── Orchestrator mode (no INTERVIEW_ID set) ──────────────────────────────────
# No Chrome or PulseAudio needed — skip the entire audio/display setup.
if [ -z "$INTERVIEW_ID" ]; then
    log "Orchestrator mode — skipping display/audio setup"
    exec python3 /app/orchestrator.py
fi

# ── Single-interview mode ─────────────────────────────────────────────────────
# Each spawned container runs exactly one interview then exits.
log "Single-interview mode: INTERVIEW_ID=$INTERVIEW_ID"

export DISPLAY=:99
export PULSE_SERVER=unix:/var/run/pulse/native

# Clean stale locks from any previous run in this container
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
pkill -9 Xvfb pulseaudio 2>/dev/null || true

# ── Xvfb (virtual display for Chrome) ────────────────────────────────────────
log "Starting Xvfb :99..."
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset -nolisten tcp &
sleep 1
log "Xvfb running"

# ── PulseAudio ────────────────────────────────────────────────────────────────
log "Starting PulseAudio..."
mkdir -p /var/run/pulse /root/.config/pulse
chmod 777 /var/run/pulse

cat > /etc/pulse/default.pa << 'EOF'
load-module module-native-protocol-unix auth-anonymous=1 socket=/var/run/pulse/native
load-module module-null-sink sink_name=VirtualSink sink_properties="device.description=VirtualSink"
load-module module-remap-source source_name=BotMic master=VirtualSink.monitor source_properties="device.description=BotMicCapture"
load-module module-null-sink sink_name=virtual_mic sink_properties="device.description=VirtualMic"
load-module module-remap-source source_name=virtual_mic_source master=virtual_mic.monitor source_properties="device.description=VirtualMicSource"
set-default-sink VirtualSink
set-default-source virtual_mic_source
EOF

pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit --use-pid-file=false 2>&1 \
    || log "PulseAudio start returned non-zero (may still be running)"
sleep 2

if pactl info &>/dev/null; then
    log "PulseAudio running"
    pactl set-sink-volume VirtualSink 150%   || true
    pactl set-sink-volume virtual_mic 150%   || true
    pactl set-source-volume virtual_mic_source 150% || true
else
    log "WARNING: PulseAudio not responding — retrying..."
    pulseaudio --daemonize=yes --exit-idle-time=-1 2>&1 || true
    sleep 2
fi

# Keep virtual_mic alive so Chrome WebRTC doesn't suspend it
pacat --playback --device=virtual_mic --format=s16le --rate=22050 --channels=1 \
    < /dev/zero &

# ── Chrome audio policy ───────────────────────────────────────────────────────
mkdir -p /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/allow_audio.json << 'EOF'
{"AudioCaptureAllowed": true, "AudioCaptureAllowedUrls": ["meet.google.com", "https://meet.google.com"]}
EOF

cat > /etc/asound.conf << 'EOF'
pcm.pulse { type pulse }
ctl.pulse { type pulse }
pcm.!default { type pulse }
ctl.!default { type pulse }
EOF

# ── Start local TTS listener then the bot ────────────────────────────────────
log "Starting local TTS listener..."
python3 /app/tts_listener.py &
log "TTS listener started (PID=$!)"

log "Starting bot for $INTERVIEW_ID..."
exec python3 /app/main.py
