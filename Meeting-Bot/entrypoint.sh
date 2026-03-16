#!/bin/bash
set -e

export DISPLAY=:99
export PULSE_SERVER=unix:/var/run/pulse/native
export PYTHONUNBUFFERED=1

log() { echo "[$(date '+%H:%M:%S')] $1"; }

# Clean stale locks
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
pkill -9 Xvfb pulseaudio 2>/dev/null || true

# Start Xvfb (fast)
log "Starting Xvfb :99..."
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset -nolisten tcp &
sleep 1
log "✅ Xvfb running"

# Start PulseAudio as regular daemon (not system mode)
log "Starting PulseAudio..."
mkdir -p /var/run/pulse /root/.config/pulse
chmod 777 /var/run/pulse

# Configure PulseAudio for anonymous connections (TTS container needs this)
mkdir -p /etc/pulse
cat > /etc/pulse/default.pa << 'EOF'
# Load native protocol module for TTS container
load-module module-native-protocol-unix auth-anonymous=1 socket=/var/run/pulse/native

# Create virtual microphone sink (TTS output → Meet input)
load-module module-null-sink sink_name=virtual_mic sink_properties="device.description=VirtualMic device.class=sound"

# Create monitor source from virtual mic (for STT to capture)
load-module module-remap-source source_name=virtual_mic_source master=virtual_mic.monitor source_properties="device.description=BotMicCapture device.class=sound"

# Create loopback from system audio to virtual mic (Meet audio → STT)
load-module module-loopback source=auto_null sink=virtual_mic latency_msec=10

# Set defaults
set-default-sink virtual_mic
set-default-source virtual_mic_source

# Increase default sample rate for better quality
set-default-sample-rate 48000
EOF

# Start PulseAudio
pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit --use-pid-file=false 2>&1 || log "⚠️  PulseAudio start returned non-zero"
sleep 2

# Verify PulseAudio is running
if pactl info &>/dev/null; then
    log "✅ PulseAudio running"
    # List all sources and sinks for debugging
    log "PulseAudio sources:"
    pactl list sources short 2>&1 | while read line; do log "  $line"; done
    log "PulseAudio sinks:"
    pactl list sinks short 2>&1 | while read line; do log "  $line"; done
else
    log "❌ PulseAudio NOT running - trying fallback..."
    pulseaudio --daemonize=yes --exit-idle-time=-1 2>&1 || true
    sleep 1
fi

# Chrome audio policy (parallel)
log "Writing Chrome audio policy..."
mkdir -p /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/allow_audio.json << 'EOF'
{
  "AudioCaptureAllowed": true,
  "AudioCaptureAllowedUrls": ["meet.google.com", "https://meet.google.com"]
}
EOF

# ALSA config (parallel)
cat > /etc/asound.conf << 'EOF'
pcm.pulse { type pulse }
ctl.pulse { type pulse }
pcm.!default { type pulse }
ctl.!default { type pulse }
EOF

log "✅ Ready - Starting Meeting Bot..."
exec python3 main.py
