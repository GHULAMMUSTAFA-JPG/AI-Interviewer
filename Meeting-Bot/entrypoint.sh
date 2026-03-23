#!/bin/bash
set -e

export DISPLAY=:99
export PULSE_SERVER=unix:/var/run/pulse/native
export PYTHONUNBUFFERED=1
export VNC_PASSWORD=meetingbot123

log() { echo "[$(date '+%H:%M:%S')] $1"; }

# Clean stale locks
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
pkill -9 Xvfb pulseaudio x11vnc 2>/dev/null || true

# Start Xvfb (virtual display for Chrome)
log "Starting Xvfb :99..."
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset -nolisten tcp &
sleep 1
log "✅ Xvfb running"

# Start VNC server for remote desktop access (debugging)
log "Starting VNC server on port 5900..."
# Create VNC password file
x11vnc -storepasswd $VNC_PASSWORD /tmp/vncpass 2>/dev/null
# Start VNC with password auth
x11vnc -display :99 -forever -shared -rfbauth /tmp/vncpass -listen 0.0.0.0 -o /var/log/x11vnc.log &
sleep 1
log "✅ VNC server running on port 5900 (password: $VNC_PASSWORD)"

# Start noVNC web server (web-based VNC viewer - no download needed!)
log "Starting noVNC web server on port 6080..."
/app/venv/bin/python -m websockify --web=/usr/share/novnc 6080 localhost:5900 > /var/log/websockify.log 2>&1 &
sleep 2
log "✅ noVNC available at http://localhost:6080/vnc.html"

# Start PulseAudio - MATCH OTHER PROJECT'S CONFIG
log "Starting PulseAudio..."
mkdir -p /var/run/pulse /root/.config/pulse
chmod 777 /var/run/pulse

# Configure PulseAudio (exact same as other project)
mkdir -p /etc/pulse
cat > /etc/pulse/default.pa << 'EOF'
# Load native protocol for TTS container
load-module module-native-protocol-unix auth-anonymous=1 socket=/var/run/pulse/native

# Create virtual devices (exact same as other project)
load-module module-null-sink sink_name=VirtualSink sink_properties="device.description=VirtualSink"
load-module module-remap-source source_name=BotMic master=VirtualSink.monitor source_properties="device.description=BotMicCapture"
load-module module-null-sink sink_name=virtual_mic sink_properties="device.description=VirtualMic"
load-module module-remap-source source_name=virtual_mic_source master=virtual_mic.monitor source_properties="device.description=VirtualMicSource"

# Set defaults (exact same as other project)
set-default-sink VirtualSink
set-default-source virtual_mic_source

# Keep virtual_mic running (prevents Chrome WebRTC suspension)
EOF

# Start PulseAudio
pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit --use-pid-file=false 2>&1 || log "⚠️  PulseAudio start returned non-zero"
sleep 2

# Verify PulseAudio is running
if pactl info &>/dev/null; then
    log "✅ PulseAudio running"
    # Show device list for debugging
    log "PulseAudio sources:"
    pactl list sources short 2>&1 | while read line; do log "  $line"; done
    log "PulseAudio sinks:"
    pactl list sinks short 2>&1 | while read line; do log "  $line"; done
else
    log "❌ PulseAudio NOT running - trying fallback..."
    pulseaudio --daemonize=yes --exit-idle-time=-1 2>&1 || true
    sleep 1
fi

# AFTER PulseAudio starts, start keep-alive for virtual_mic (matches other project)
log "Starting virtual_mic keep-alive (silence)..."
pacat --playback --device=virtual_mic \
      --format=s16le --rate=22050 --channels=1 \
      < /dev/zero &

# Chrome audio policy
log "Writing Chrome audio policy..."
mkdir -p /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/allow_audio.json << 'EOF'
{
  "AudioCaptureAllowed": true,
  "AudioCaptureAllowedUrls": ["meet.google.com", "https://meet.google.com"]
}
EOF

# ALSA config
cat > /etc/asound.conf << 'EOF'
pcm.pulse { type pulse }
ctl.pulse { type pulse }
pcm.!default { type pulse }
ctl.!default { type pulse }
EOF

log "✅ Ready - Starting Meeting Bot..."
exec python3 main.py
