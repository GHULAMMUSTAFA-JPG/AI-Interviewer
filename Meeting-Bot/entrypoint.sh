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
log "✅ Xvfb running"

# Start PulseAudio as regular daemon (not system mode)
log "Starting PulseAudio..."
mkdir -p /var/run/pulse /root/.config/pulse
chmod 777 /var/run/pulse

# Configure PulseAudio for anonymous connections (TTS container needs this)
mkdir -p /etc/pulse
cat > /etc/pulse/default.pa << 'EOF'
load-module module-native-protocol-unix auth-anonymous=1 socket=/var/run/pulse/native
load-module module-null-sink sink_name=virtual_mic sink_properties=device.description=VirtualMic
load-module module-remap-source source_name=virtual_mic_source master=virtual_mic.monitor source_properties=device.description=VirtualMicSource
set-default-sink virtual_mic
set-default-source virtual_mic_source
EOF

# Start PulseAudio
pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit --use-pid-file=false 2>&1 || log "⚠️  PulseAudio start returned non-zero"
sleep 3

# Verify PulseAudio is running
if pactl info &>/dev/null; then
    log "✅ PulseAudio running"

    # Show devices for debugging
    log "Sinks:"
    pactl list short sinks 2>/dev/null || true
    log "Sources:"
    pactl list short sources 2>/dev/null || true
    
    # Verify default sink/source
    log "Default sink:"
    pactl get-default-sink 2>/dev/null || true
    log "Default source:"
    pactl get-default-source 2>/dev/null || true
else
    log "❌ PulseAudio NOT running - checking process..."
    ps aux 2>/dev/null | grep -i pulse || true
    log "Trying alternative start method..."

    # Alternative: start without config
    pulseaudio --daemonize=yes --exit-idle-time=-1 2>&1 || true
    sleep 2

    if pactl info &>/dev/null; then
        log "✅ PulseAudio started with fallback method"
    else
        log "❌ PulseAudio failed completely"
    fi
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

log "Starting Meeting Bot..."
exec python3 main.py
