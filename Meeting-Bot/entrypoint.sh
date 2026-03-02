#!/bin/bash
# ════════════════════════════════════════════════════════════════════════════════
# Meeting-Bot Entrypoint — Robust VNC + Xvfb + PulseAudio Setup
# ════════════════════════════════════════════════════════════════════════════════
#
# This script initializes the complete desktop environment for the headless
# Chrome browser that joins Google Meet. It handles:
#
#   1. Xvfb (X Virtual Framebuffer) — provides a virtual display :99
#   2. x11vnc — VNC server that exposes the Xvfb display on port 5900
#   3. noVNC + websockify — WebSocket proxy for browser-based VNC on port 6080
#   4. PulseAudio — Virtual audio sink for TTS → Chrome microphone routing
#   5. Health monitoring — Automatic restart of failed components
#   6. Debug logging — Comprehensive status output for troubleshooting
#
# Architecture:
#
#   ┌─────────────────────────────────────────────────────────────────────┐
#   │  Xvfb Display :99 (1280x720x24)                                     │
#   │  ┌───────────────────────────────────────────────────────────────┐  │
#   │  │  Google Chrome (Chromium)                                     │  │
#   │  │  - Joins Google Meet                                          │  │
#   │  │  - Receives TTS audio via PulseAudio virtual_mic_source       │  │
#   │  │  - Captures captions from DOM                                 │  │
#   │  └───────────────────────────────────────────────────────────────┘  │
#   └─────────────────────────────────────────────────────────────────────┘
#                          │
#                          ▼
#   ┌─────────────────────────────────────────────────────────────────────┐
#   │  x11vnc (port 5900) ← captures Xvfb display                         │
#   └─────────────────────────────────────────────────────────────────────┘
#                          │
#                          ▼
#   ┌─────────────────────────────────────────────────────────────────────┐
#   │  websockify (port 6080) ← WebSocket proxy to VNC                    │
#   └─────────────────────────────────────────────────────────────────────┘
#                          │
#                          ▼
#   ┌─────────────────────────────────────────────────────────────────────┐
#   │  noVNC web server — Browser access at http://localhost:6080/        │
#   └─────────────────────────────────────────────────────────────────────┘
#
#   ┌─────────────────────────────────────────────────────────────────────┐
#   │  PulseAudio (system mode)                                           │
#   │  ┌───────────────────────────────────────────────────────────────┐  │
#   │  │  virtual_mic (null-sink) ← TTS audio from TTS container       │  │
#   │  │  virtual_mic_source ← Chrome reads this as microphone input   │  │
#   │  └───────────────────────────────────────────────────────────────┘  │
#   └─────────────────────────────────────────────────────────────────────┘
#
# Usage:
#   docker run --env-file .env meeting-bot:latest
#   OR
#   docker compose up meeting-bot
#
# VNC Access:
#   Browser: http://localhost:6080/ (auto-connects to VNC)
#   VNC Client: localhost:5900 (no password)
#
# ════════════════════════════════════════════════════════════════════════════════

set -e  # Exit on first error (we want to know if setup fails)

# ════════════════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════════════════

export DISPLAY=:99
export PULSE_SERVER=unix:/var/run/pulse/native
export PYTHONUNBUFFERED=1

XVFB_DISPLAY=":99"
XVFB_SCREEN="1280x720x24"
XVFB_SCREEN_DEPTH="24"
VNC_PORT="5900"
NOVNC_PORT="6080"
PULSE_SOCKET="/var/run/pulse/native"
RECORDINGS_DIR="/app/recordings"
LOG_FILE="${RECORDINGS_DIR}/entrypoint.log"

# ════════════════════════════════════════════════════════════════════════════════
# Logging Functions
# ════════════════════════════════════════════════════════════════════════════════

log_info() {
    local msg="[INFO] $(date '+%Y-%m-%d %H:%M:%S') - $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

log_error() {
    local msg="[ERROR] $(date '+%Y-%m-%d %H:%M:%S') - $1"
    echo "$msg" >&2
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

log_success() {
    local msg="[OK] $(date '+%Y-%m-%d %H:%M:%S') - $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

# ════════════════════════════════════════════════════════════════════════════════
# Cleanup Function — Remove stale locks and processes
# ════════════════════════════════════════════════════════════════════════════════

cleanup_stale_resources() {
    log_info "=== Cleaning up stale resources ==="
    
    # Remove X11 lock files
    rm -f /tmp/.X${XVFB_DISPLAY#:}-lock 2>/dev/null || true
    rm -f /tmp/.X11-unix/X${XVFB_DISPLAY#:} 2>/dev/null || true
    rm -f /tmp/.X*-lock 2>/dev/null || true
    
    # Kill any existing Xvfb, x11vnc, or pulseaudio processes
    pkill -9 Xvfb 2>/dev/null || true
    pkill -9 x11vnc 2>/dev/null || true
    pkill -9 pulseaudio 2>/dev/null || true
    pkill -9 websockify 2>/dev/null || true
    
    # Fix PulseAudio runtime directory.
    # /var/run/pulse is a Docker volume so it cannot be deleted, but the .config
    # subdirectory inside it can be created during image build with the wrong UID
    # (106 instead of pulse=105). This causes module-native-protocol-unix to fail
    # loading the auth cookie and never create the native socket.
    # Fix: clear stale files, fix ownership so pulse user can write.
    rm -f /var/run/pulse/native /var/run/pulse/pid 2>/dev/null || true
    rm -rf /var/run/pulse/.config 2>/dev/null || true
    chown -R pulse:pulse /var/run/pulse 2>/dev/null || true
    chmod 755 /var/run/pulse
    
    # Clean Chrome profile locks (prevents "profile in use" errors)
    if [ -d "/app/chrome_profile" ]; then
        rm -f /app/chrome_profile/SingletonLock 2>/dev/null || true
        rm -f /app/chrome_profile/SingletonCookie 2>/dev/null || true
        rm -f /app/chrome_profile/SingletonSocket 2>/dev/null || true
    fi
    
    log_success "Cleanup complete"
}

# ════════════════════════════════════════════════════════════════════════════════
# Xvfb Setup — Virtual Display Server
# ════════════════════════════════════════════════════════════════════════════════

start_xvfb() {
    log_info "=== Starting Xvfb (X Virtual Framebuffer) ==="
    log_info "Display: $XVFB_DISPLAY"
    log_info "Screen: $XVFB_SCREEN"
    log_info "Color depth: $XVFB_SCREEN_DEPTH"
    
    # Start Xvfb with extended GLX support (required for Chrome hardware acceleration)
    Xvfb "$XVFB_DISPLAY" \
        -screen 0 "$XVFB_SCREEN" \
        -ac \
        +extension GLX \
        +render \
        -noreset \
        -nolisten tcp \
        &
    
    XVFB_PID=$!
    
    # Wait for Xvfb to be ready
    sleep 2
    
    # Verify Xvfb is running
    if kill -0 $XVFB_PID 2>/dev/null; then
        log_success "Xvfb started successfully (PID: $XVFB_PID)"
    else
        log_error "Xvfb failed to start or crashed immediately"
        exit 1
    fi
    
    # Export DISPLAY for subsequent commands
    export DISPLAY="$XVFB_DISPLAY"
}

# ════════════════════════════════════════════════════════════════════════════════
# Desktop Background — Visual confirmation that VNC is working
# ════════════════════════════════════════════════════════════════════════════════

set_desktop_background() {
    log_info "=== Setting desktop background ==="
    
    # Set a solid color background (visible proof that VNC is connected)
    # Color: #1e3a5f (dark blue) — matches the UI theme
    if xsetroot -display "$XVFB_DISPLAY" -solid '#1e3a5f' 2>/dev/null; then
        log_success "Desktop background set (dark blue #1e3a5f)"
    else
        log_error "Failed to set desktop background (xsetroot may not be installed)"
    fi
    
    # Optional: Add a text label using xterm (if available)
    # This helps identify which container/environment you're viewing
    if command -v xterm &>/dev/null; then
        xterm -display "$XVFB_DISPLAY" \
              -geometry 80x5+10+10 \
              -bg "#1e3a5f" \
              -fg "#ffffff" \
              -e "echo 'Meeting Bot - Display :99'; sleep infinity" &
        log_info "Desktop label added (xterm)"
    fi
}

# ════════════════════════════════════════════════════════════════════════════════
# x11vnc Setup — VNC Server
# ════════════════════════════════════════════════════════════════════════════════

start_x11vnc() {
    log_info "=== Starting x11vnc (VNC Server) ==="
    log_info "Display: $XVFB_DISPLAY"
    log_info "Port: $VNC_PORT"
    log_info "Authentication: None (open access)"
    
    # Use flags known to work — avoid undocumented or version-specific flags.
    # -bg is intentionally excluded: it forks a daemon and exits the parent,
    # so $! would point to an already-dead PID, making health checks useless.
    x11vnc \
        -display "$XVFB_DISPLAY" \
        -nopw \
        -forever \
        -shared \
        -listen 0.0.0.0 \
        -rfbport "$VNC_PORT" \
        -q \
        -xkb \
        2>/dev/null &

    VNC_PID=$!

    # Wait for x11vnc to be ready
    sleep 1

    # Check by process name — pgrep is reliable regardless of fork behaviour.
    if pgrep -x x11vnc > /dev/null 2>&1; then
        log_success "x11vnc started successfully (Port: $VNC_PORT)"
    else
        log_error "x11vnc failed to start"
        exit 1
    fi
}

# ════════════════════════════════════════════════════════════════════════════════
# noVNC Setup — Web-based VNC Client
# ════════════════════════════════════════════════════════════════════════════════

start_novnc() {
    log_info "=== Starting noVNC (Web-based VNC Client) ==="
    log_info "Web server port: $NOVNC_PORT"
    log_info "VNC proxy target: localhost:$VNC_PORT"
    
    # Verify noVNC installation
    if [ ! -d "/opt/novnc" ]; then
        log_error "noVNC not found at /opt/novnc — installation may be incomplete"
        exit 1
    fi
    
    # Start websockify with embedded noVNC web server
    # --wrap-mode=ignore: Disable SSL wrapping (we use HTTP internally)
    # The noVNC web server serves index.html which auto-connects to VNC
    websockify \
        --web=/opt/novnc \
        --wrap-mode=ignore \
        --heartbeat=30 \
        "$NOVNC_PORT" \
        localhost:"$VNC_PORT" \
        &
    
    NOVNC_PID=$!
    
    # Wait for websockify to be ready
    sleep 1
    
    # Verify websockify is running
    if kill -0 $NOVNC_PID 2>/dev/null; then
        log_success "noVNC started successfully (PID: $NOVNC_PID, Port: $NOVNC_PORT)"
        log_info "Access VNC at: http://localhost:$NOVNC_PORT/"
    else
        log_error "websockify/noVNC failed to start"
        exit 1
    fi
}

# ════════════════════════════════════════════════════════════════════════════════
# PulseAudio Setup — Virtual Audio Sink for TTS
# ════════════════════════════════════════════════════════════════════════════════

start_pulseaudio() {
    log_info "=== Starting PulseAudio (System Mode) ==="
    log_info "Socket: $PULSE_SOCKET"
    log_info "Virtual mic: virtual_mic"
    
    # Create PulseAudio socket directory
    mkdir -p /var/run/pulse
    mkdir -p /root/.config/pulse
    
    # Start PulseAudio in system mode (required for root in Docker)
    # --system: Run in system mode (accessible by all users)
    # --daemonize=yes: Run as background daemon
    # --exit-idle-time=-1: Never exit due to inactivity
    # --disallow-exit: Prevent accidental termination
    pulseaudio \
        --system \
        --daemonize=yes \
        --exit-idle-time=-1 \
        --disallow-exit \
        2>/dev/null || true
    
    # Wait for PulseAudio to initialize
    sleep 2
    
    # Verify PulseAudio is running
    if pactl info &>/dev/null; then
        log_success "PulseAudio started successfully"
    else
        log_error "PulseAudio failed to start"
        # Continue anyway — TTS will handle this gracefully
    fi
    
    # Set environment variable for PulseAudio connection
    export PULSE_SERVER="unix:$PULSE_SOCKET"
}

# ════════════════════════════════════════════════════════════════════════════════
# Virtual Audio Sink — Route TTS Audio to Chrome Microphone
# ════════════════════════════════════════════════════════════════════════════════

setup_virtual_audio() {
    log_info "=== Setting up Virtual Audio Sink ==="
    
    # Wait for PulseAudio to be fully ready
    sleep 1
    
    # Load null-sink module (virtual speaker that TTS writes to)
    # This creates a "speaker" that doesn't output anywhere — it's a loopback
    log_info "Creating virtual_mic null-sink..."
    if pactl load-module module-null-sink \
        sink_name=virtual_mic \
        sink_properties=device.description=VirtualMic \
        2>/dev/null; then
        log_success "virtual_mic null-sink created"
    else
        log_error "Failed to create virtual_mic null-sink"
    fi
    
    # Load virtual-source module (makes the null-sink monitor available as a source)
    # Chrome will read from this as its microphone input
    log_info "Creating virtual_mic_source..."
    if pactl load-module module-virtual-source \
        source_name=virtual_mic_source \
        master=virtual_mic.monitor \
        2>/dev/null; then
        log_success "virtual_mic_source created"
    else
        log_error "Failed to create virtual_mic_source"
    fi
    
    # Set default sink (system-wide default output)
    log_info "Setting virtual_mic as default sink..."
    if pactl set-default-sink virtual_mic 2>/dev/null; then
        log_success "Default sink set to virtual_mic"
    else
        log_error "Failed to set default sink"
    fi
    
    # Set default source (system-wide default input)
    log_info "Setting virtual_mic_source as default source..."
    if pactl set-default-source virtual_mic_source 2>/dev/null; then
        log_success "Default source set to virtual_mic_source"
    else
        log_error "Failed to set default source"
    fi
    
    # Log audio configuration for debugging
    log_info "=== PulseAudio Configuration ==="
    pactl info 2>&1 | while read line; do
        log_info "  $line"
    done
}

# ════════════════════════════════════════════════════════════════════════════════
# Health Check — Monitor Component Status
# ════════════════════════════════════════════════════════════════════════════════

start_health_monitor() {
    log_info "=== Starting Health Monitor ==="
    
    # Background loop that checks component health every 30 seconds
    (
        while true; do
            sleep 30
            
            # Check by process name — immune to fork/daemon PID changes.
            if ! pgrep -x Xvfb > /dev/null 2>&1; then
                log_error "Xvfb crashed!"
            fi
            if ! pgrep -x x11vnc > /dev/null 2>&1; then
                log_error "x11vnc crashed!"
            fi
            if ! pgrep -f websockify > /dev/null 2>&1; then
                log_error "websockify crashed!"
            fi

            XVFB_ST=$(pgrep -x Xvfb > /dev/null 2>&1 && echo 'OK' || echo 'DEAD')
            VNC_ST=$(pgrep -x x11vnc > /dev/null 2>&1 && echo 'OK' || echo 'DEAD')
            NOVNC_ST=$(pgrep -f websockify > /dev/null 2>&1 && echo 'OK' || echo 'DEAD')
            log_info "Health check: Xvfb=$XVFB_ST, VNC=$VNC_ST, noVNC=$NOVNC_ST"
        done
    ) &
    
    HEALTH_PID=$!
    log_success "Health monitor started (PID: $HEALTH_PID)"
}

# ════════════════════════════════════════════════════════════════════════════════
# Startup Banner — Display connection information
# ════════════════════════════════════════════════════════════════════════════════

show_startup_banner() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════════════════════════╗"
    echo "║           Meeting Bot — VNC Environment Ready                                 ║"
    echo "╠═══════════════════════════════════════════════════════════════════════════════╣"
    echo "║  VNC Access:                                                                  ║"
    printf "║    Browser:     http://localhost:%-5s/                                  ║\n" "$NOVNC_PORT"
    printf "║    VNC Client:  localhost:%-5s (no password)                          ║\n" "$VNC_PORT"
    echo "║                                                                               ║"
    echo "║  Audio Routing:                                                               ║"
    echo "║    TTS → virtual_mic → virtual_mic_source → Chrome → Google Meet             ║"
    echo "║                                                                               ║"
    echo "║  Process IDs:                                                                 ║"
    printf "║    Xvfb:      %-8s                                                    ║\n" "$XVFB_PID"
    printf "║    x11vnc:    %-8s                                                    ║\n" "$VNC_PID"
    printf "║    noVNC:     %-8s                                                    ║\n" "$NOVNC_PID"
    printf "║    Health:    %-8s                                                    ║\n" "$HEALTH_PID"
    echo "║                                                                               ║"
    echo "║  Logs: /app/recordings/entrypoint.log                                         ║"
    echo "╚═══════════════════════════════════════════════════════════════════════════════╝"
    echo ""
}

# ════════════════════════════════════════════════════════════════════════════════
# Main Execution
# ════════════════════════════════════════════════════════════════════════════════

main() {
    # Create recordings directory for logs
    mkdir -p "$RECORDINGS_DIR"
    chmod 777 "$RECORDINGS_DIR"
    
    # Initialize log file
    echo "=== Meeting Bot Entrypoint Log ===" > "$LOG_FILE"
    echo "Started: $(date)" >> "$LOG_FILE"
    echo "" >> "$LOG_FILE"
    
    log_info "Starting Meeting Bot environment..."
    
    # Step 1: Clean up any stale resources from previous runs
    cleanup_stale_resources
    
    # Step 2: Start Xvfb (virtual display)
    start_xvfb
    
    # Step 3: Set desktop background (visual confirmation)
    set_desktop_background
    
    # Step 4: Start x11vnc (VNC server)
    start_x11vnc
    
    # Step 5: Start noVNC (web-based VNC client)
    start_novnc
    
    # Step 6: Start PulseAudio (audio server)
    start_pulseaudio
    
    # Step 7: Configure virtual audio sink
    setup_virtual_audio
    
    # Step 8: Start health monitor
    start_health_monitor
    
    # Step 9: Show startup banner
    show_startup_banner
    
    log_success "=== Environment setup complete ==="
    log_info "Starting Meeting Bot application..."
    
    # Execute the main Python application
    # This replaces the current shell process (exec) so signals are handled correctly
    exec python3 main.py
}

# Run main function
main "$@"
