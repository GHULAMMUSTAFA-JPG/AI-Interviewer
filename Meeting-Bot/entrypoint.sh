#!/bin/bash
# Meeting-Bot entrypoint
# Starts Xvfb + x11vnc + noVNC + PulseAudio before Python.
#
# VNC:   x11vnc captures Xvfb :99, exposes it on port 5900 (no password)
# noVNC: websockify proxies port 6080 → 5900; open http://localhost:6080/vnc.html
#
# PulseAudio runs in --system mode (required for root in Docker).
# system.pa is patched at build time with auth-anonymous=1 so that
# pacat (TTS) can connect to the socket without group auth.
# Socket at /var/run/pulse/native (pulse-socket Docker volume).

set -e

echo "[entrypoint] Cleaning up stale locks..."
rm -f /tmp/.X99-lock /tmp/.X*-lock 2>/dev/null || true
pkill pulseaudio 2>/dev/null || true
pkill x11vnc    2>/dev/null || true
rm -f /var/run/pulse/native /var/run/pulse/pid 2>/dev/null || true

echo "[entrypoint] Starting Xvfb on display :99..."
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99
sleep 1

echo "[entrypoint] Setting desktop background..."
xsetroot -display :99 -solid '#1e3a5f' || true   # dark blue — visible proof VNC is connected

echo "[entrypoint] Starting x11vnc on port 5900..."
x11vnc -display :99 -nopw -forever -shared \
    -listen 0.0.0.0 -rfbport 5900 \
    -bg -q -xkb 2>/dev/null || true

echo "[entrypoint] Starting noVNC on port 6080..."
# index.html is baked into /opt/novnc at build time (no runtime copy needed).
websockify --web=/opt/novnc \
    --wrap-mode=ignore \
    6080 localhost:5900 &

echo "[entrypoint] Starting PulseAudio (system mode)..."
pulseaudio --system --daemonize=yes --exit-idle-time=-1 --disallow-exit
sleep 2

echo "[entrypoint] Creating virtual mic sink..."
export PULSE_SERVER=unix:/var/run/pulse/native

pactl load-module module-null-sink \
  sink_name=virtual_mic sink_properties=device.description=VirtualMic || true

pactl load-module module-virtual-source \
  source_name=virtual_mic_source master=virtual_mic.monitor || true

pactl set-default-sink virtual_mic || true
pactl set-default-source virtual_mic_source || true

echo "[entrypoint] VNC ready — open http://localhost:6080/ (auto-connects, scales to fit)"
echo "[entrypoint] PulseAudio ready. Starting Meeting-Bot..."
exec python3 main.py
