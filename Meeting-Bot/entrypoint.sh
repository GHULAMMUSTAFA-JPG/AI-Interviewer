#!/bin/bash
set -e

export DISPLAY=:99
export PULSE_SERVER=unix:/var/run/pulse/native
export PYTHONUNBUFFERED=1

log() { echo "[$(date '+%H:%M:%S')] $1"; }

# Clean stale locks
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
pkill -9 Xvfb pulseaudio 2>/dev/null || true
sleep 1

# Start Xvfb
log "Starting Xvfb :99..."
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset -nolisten tcp &
sleep 2
log "Xvfb running"

# Start PulseAudio (for TTS container)
log "Starting PulseAudio..."
mkdir -p /var/run/pulse /root/.config/pulse
chmod 777 /var/run/pulse

pulseaudio --system --daemonize=yes --exit-idle-time=-1 --disallow-exit 2>/dev/null || true
sleep 2

if pactl info &>/dev/null; then
    log "PulseAudio running"
    pactl load-module module-null-sink sink_name=virtual_mic sink_properties=device.description=VirtualMic 2>/dev/null && log "virtual_mic created" || true
    pactl load-module module-remap-source source_name=virtual_mic_source master=virtual_mic.monitor source_properties=device.description=VirtualMicSource 2>/dev/null && log "virtual_mic_source created" || true
    pactl set-default-sink virtual_mic 2>/dev/null || true
    pactl set-default-source virtual_mic_source 2>/dev/null || true
    log "10"
    pactl list short sinks 2>/dev/null | head -5 || true
    log "11"
    pactl list short sources 2>/dev/null | head -5 || true
else
    log "PulseAudio failed to start - TTS will handle gracefully"
fi

# Chrome audio policy
log "Writing Chrome audio capture policy..."
mkdir -p /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/allow_audio.json << 'EOF'
{
  "AudioCaptureAllowed": true,
  "AudioCaptureAllowedUrls": ["meet.google.com", "https://meet.google.com"]
}
EOF
log "✅ Chrome policy written"

# ALSA config
cat > /etc/asound.conf << 'EOF'
pcm.pulse { type pulse }
ctl.pulse { type pulse }
pcm.!default { type pulse }
ctl.!default { type pulse }
EOF
log "✅ ALSA config written"

log "Environment ready - starting Meeting Bot..."
exec python3 main.py
