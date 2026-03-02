#!/bin/bash
# ════════════════════════════════════════════════════════════════════════════════
# VNC Health Check & Troubleshooting Script
# ════════════════════════════════════════════════════════════════════════════════
#
# This script provides diagnostic commands for troubleshooting VNC issues.
# Run it inside the container to check the health of all VNC components.
#
# Usage:
#   docker compose exec meeting-bot /app/vnc_healthcheck.sh
#
# ════════════════════════════════════════════════════════════════════════════════

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ════════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ════════════════════════════════════════════════════════════════════════════════

print_header() {
    echo -e "\n${BLUE}════════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════════════${NC}\n"
}

print_status() {
    local status=$1
    local message=$2
    
    if [ "$status" = "OK" ]; then
        echo -e "  ${GREEN}✓${NC} $message"
    elif [ "$status" = "WARN" ]; then
        echo -e "  ${YELLOW}⚠${NC} $message"
    elif [ "$status" = "FAIL" ]; then
        echo -e "  ${RED}✗${NC} $message"
    else
        echo -e "  $message"
    fi
}

check_process() {
    local name=$1
    local pattern=$2
    
    if pgrep -f "$pattern" > /dev/null 2>&1; then
        local pid=$(pgrep -f "$pattern" | head -1)
        print_status "OK" "$name is running (PID: $pid)"
        return 0
    else
        print_status "FAIL" "$name is NOT running"
        return 1
    fi
}

check_port() {
    local name=$1
    local port=$2
    
    if netstat -tuln 2>/dev/null | grep -q ":$port " || \
       ss -tuln 2>/dev/null | grep -q ":$port "; then
        print_status "OK" "$name listening on port $port"
        return 0
    else
        print_status "FAIL" "$name NOT listening on port $port"
        return 1
    fi
}

# ════════════════════════════════════════════════════════════════════════════════
# Main Diagnostics
# ════════════════════════════════════════════════════════════════════════════════

print_header "VNC Health Check"

echo "Timestamp: $(date)"
echo "Hostname: $(hostname)"
echo ""

# ════════════════════════════════════════════════════════════════════════════════
# Xvfb Check
# ════════════════════════════════════════════════════════════════════════════════

print_header "1. Xvfb (Virtual Display)"

check_process "Xvfb" "Xvfb.*:99"
if [ $? -eq 0 ]; then
    # Check if display :99 exists
    if [ -e "/tmp/.X11-unix/X99" ]; then
        print_status "OK" "Display :99 socket exists"
    else
        print_status "WARN" "Display :99 socket not found"
    fi
    
    # Try to query the display
    if DISPLAY=:99 xset q &>/dev/null; then
        print_status "OK" "Display :99 is responsive"
    else
        print_status "FAIL" "Display :99 is not responsive"
    fi
fi

# ════════════════════════════════════════════════════════════════════════════════
# x11vnc Check
# ════════════════════════════════════════════════════════════════════════════════

print_header "2. x11vnc (VNC Server)"

check_process "x11vnc" "x11vnc"
check_port "x11vnc" "5900"

# Check VNC connection
if timeout 2 bash -c 'cat < /dev/null > /dev/tcp/localhost/5900' 2>/dev/null; then
    print_status "OK" "VNC port 5900 is accessible"
else
    print_status "FAIL" "VNC port 5900 is not accessible"
fi

# ════════════════════════════════════════════════════════════════════════════════
# noVNC / Websockify Check
# ════════════════════════════════════════════════════════════════════════════════

print_header "3. noVNC / Websockify (Web Access)"

check_process "websockify" "websockify"
check_port "noVNC web server" "6080"

# Check noVNC files
if [ -d "/opt/novnc" ]; then
    print_status "OK" "noVNC directory exists at /opt/novnc"
    
    if [ -f "/opt/novnc/vnc.html" ]; then
        print_status "OK" "vnc.html exists"
    else
        print_status "FAIL" "vnc.html not found"
    fi
    
    if [ -f "/opt/novnc/index.html" ]; then
        print_status "OK" "index.html exists (landing page)"
    else
        print_status "WARN" "index.html not found (using vnc.html directly)"
    fi
else
    print_status "FAIL" "noVNC directory not found at /opt/novnc"
fi

# Test HTTP access
if curl -s -o /dev/null -w "%{http_code}" http://localhost:6080/ | grep -q "200\|302"; then
    print_status "OK" "Web server responding on port 6080"
else
    print_status "FAIL" "Web server NOT responding on port 6080"
fi

# ════════════════════════════════════════════════════════════════════════════════
# PulseAudio Check
# ════════════════════════════════════════════════════════════════════════════════

print_header "4. PulseAudio (Virtual Audio)"

check_process "pulseaudio" "pulseaudio"

# Check PulseAudio socket
if [ -e "/var/run/pulse/native" ]; then
    print_status "OK" "PulseAudio socket exists"
else
    print_status "FAIL" "PulseAudio socket not found"
fi

# Check PulseAudio server
if pactl info &>/dev/null; then
    print_status "OK" "PulseAudio server is responsive"
    
    # Show server info
    echo ""
    echo "  Server Info:"
    pactl info 2>&1 | sed 's/^/    /'
    
    # Check for virtual sink
    echo ""
    echo "  Sinks:"
    pactl list sinks short 2>&1 | sed 's/^/    /'
    
    # Check for virtual source
    echo ""
    echo "  Sources:"
    pactl list sources short 2>&1 | sed 's/^/    /'
else
    print_status "FAIL" "PulseAudio server not responsive"
fi

# ════════════════════════════════════════════════════════════════════════════════
# Chrome/Playwright Check
# ════════════════════════════════════════════════════════════════════════════════

print_header "5. Chrome / Playwright"

if command -v google-chrome &>/dev/null; then
    chrome_version=$(google-chrome --version 2>&1)
    print_status "OK" "Chrome installed: $chrome_version"
else
    print_status "FAIL" "Chrome not installed"
fi

if [ -d "/app/venv" ]; then
    if /app/venv/bin/playwright --version &>/dev/null; then
        pw_version=$(/app/venv/bin/playwright --version)
        print_status "OK" "Playwright installed: $pw_version"
    else
        print_status "FAIL" "Playwright not installed"
    fi
else
    print_status "FAIL" "Python venv not found"
fi

# Check Chrome profile directory
if [ -d "/app/chrome_profile" ]; then
    print_status "OK" "Chrome profile directory exists"
    
    # Check for lock files
    lock_count=$(ls -la /app/chrome_profile/Singleton* 2>/dev/null | wc -l)
    if [ "$lock_count" -gt 0 ]; then
        print_status "WARN" "Chrome lock files found ($lock_count) - may indicate unclean shutdown"
    else
        print_status "OK" "No stale Chrome lock files"
    fi
else
    print_status "WARN" "Chrome profile directory does not exist"
fi

# ════════════════════════════════════════════════════════════════════════════════
# Environment Variables Check
# ════════════════════════════════════════════════════════════════════════════════

print_header "6. Environment Variables"

echo "  DISPLAY=$DISPLAY"
if [ "$DISPLAY" = ":99" ]; then
    print_status "OK" "DISPLAY is set correctly"
else
    print_status "WARN" "DISPLAY may be incorrect (expected :99)"
fi

echo "  PULSE_SERVER=$PULSE_SERVER"
if [[ "$PULSE_SERVER" == *"unix:/var/run/pulse/native"* ]]; then
    print_status "OK" "PULSE_SERVER is set correctly"
else
    print_status "WARN" "PULSE_SERVER may be incorrect"
fi

echo "  PYTHONUNBUFFERED=$PYTHONUNBUFFERED"
if [ "$PYTHONUNBUFFERED" = "1" ]; then
    print_status "OK" "PYTHONUNBUFFERED is set correctly"
else
    print_status "WARN" "PYTHONUNBUFFERED may be incorrect"
fi

# ════════════════════════════════════════════════════════════════════════════════
# Disk Space Check
# ════════════════════════════════════════════════════════════════════════════════

print_header "7. Disk Space"

df -h /app /tmp 2>/dev/null | tail -n +2 | while read line; do
    echo "  $line"
done

# Check if recordings directory is writable
if [ -w "/app/recordings" ]; then
    print_status "OK" "/app/recordings is writable"
else
    print_status "FAIL" "/app/recordings is not writable"
fi

# ════════════════════════════════════════════════════════════════════════════════
# Recent Logs
# ════════════════════════════════════════════════════════════════════════════════

print_header "8. Recent Logs (last 20 lines)"

if [ -f "/app/recordings/entrypoint.log" ]; then
    echo "  === entrypoint.log ==="
    tail -20 /app/recordings/entrypoint.log 2>&1 | sed 's/^/  /'
else
    echo "  entrypoint.log not found"
fi

echo ""

# ════════════════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════════════════

print_header "Summary"

echo "For detailed troubleshooting, check:"
echo "  1. Container logs:    docker logs meeting-bot"
echo "  2. VNC access:        http://localhost:6080/"
echo "  3.MongoDB connection: docker compose exec mongodb mongosh"
echo ""
echo "Common fixes:"
echo "  - Restart container:  docker compose restart meeting-bot"
echo "  - Rebuild image:      docker compose build meeting-bot"
echo "  - Clear Chrome locks: docker compose exec meeting-bot rm -f /app/chrome_profile/Singleton*"
echo ""

print_status "OK" "Health check complete"
