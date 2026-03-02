# Meeting-Bot VNC System — Complete Documentation

## Overview

The Meeting-Bot uses a **headless Chrome browser** running in a Docker container with a **virtual display (Xvfb)** that can be accessed remotely via **VNC**. This allows you to visually monitor what the bot sees when it joins Google Meet interviews.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Meeting-Bot Container                                                      │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  Xvfb Display :99 (1280×720×24)                                       │  │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │  │
│  │  │  Google Chrome (Chromium)                                       │  │  │
│  │  │  - Joins Google Meet                                            │  │  │
│  │  │  - Receives TTS audio via PulseAudio                            │  │  │
│  │  │  - Scrapes captions from DOM                                    │  │  │
│  │  └─────────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                          │                                                   │
│                          ▼                                                   │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  x11vnc (VNC Server) — Port 5900                                      │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                          │                                                   │
│                          ▼                                                   │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  websockify + noVNC (Web Server) — Port 6080                          │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  PulseAudio (System Mode)                                             │  │
│  │  - virtual_mic (null-sink) ← TTS audio from TTS container             │  │
│  │  - virtual_mic_source → Chrome microphone input                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Start the Container

```bash
# Using Docker Compose (recommended)
docker compose up meeting-bot

# Or build and run manually
docker build -t meeting-bot:latest .
docker run --env-file .env \
  -p 6080:6080 -p 5900:5900 \
  -v meeting_bot_recordings:/app/recordings \
  -v pulse-socket:/var/run/pulse \
  --shm-size="2gb" \
  meeting-bot:latest
```

### 2. Access the VNC Desktop

**Option A: Web Browser (Recommended)**

Open your browser and navigate to:
```
http://localhost:6080/
```

You'll see a landing page with a "Connect to Desktop" button that auto-connects to the VNC session.

**Option B: VNC Client**

Use any VNC client (RealVNC, TightVNC, TigerVNC) to connect to:
```
Host: localhost
Port: 5900
Password: (none)
```

### 3. What You Should See

When connected, you should see:
- A **dark blue desktop** (#1e3a5f) — this confirms VNC is working
- Optionally, an **xterm window** showing "Meeting Bot - Display :99"
- When a meeting starts, **Google Chrome** will appear with the Google Meet interface

## Components

### 1. Xvfb (X Virtual Framebuffer)

**Purpose:** Provides a virtual display server without physical hardware.

**Configuration:**
- Display: `:99`
- Resolution: `1280×720`
- Color depth: `24-bit`

**Why?** Chrome requires an X11 display to render. Xvfb provides this virtually, allowing Chrome to run "headlessly" while still being capturable by VNC.

### 2. x11vnc (VNC Server)

**Purpose:** Captures the Xvfb display and exposes it via the VNC protocol.

**Configuration:**
- Port: `5900`
- Authentication: None (open access within Docker network)
- Mode: Shared (allows multiple viewers)

**Key flags:**
```bash
x11vnc -display :99 -forever -shared -nopw -listen 0.0.0.0 -rfbport 5900
```

### 3. noVNC + websockify (Web-based VNC)

**Purpose:** Provides browser-based VNC access via WebSocket.

**Configuration:**
- Web server port: `6080`
- VNC target: `localhost:5900`
- Auto-connect: Enabled
- Scaling: Fit to browser window

**Features:**
- No VNC client software required
- Works in any modern browser
- Auto-reconnect on connection loss
- Responsive scaling

### 4. PulseAudio (Virtual Audio)

**Purpose:** Routes TTS audio from the TTS container into Chrome's microphone input.

**Configuration:**
- Mode: System (for root in Docker)
- Socket: `/var/run/pulse/native`
- Virtual sink: `virtual_mic`
- Virtual source: `virtual_mic_source`

**Audio Flow:**
```
TTS Container → ElevenLabs API → PCM Audio
                                    ↓
                    pacat → virtual_mic (null-sink)
                                    ↓
                        virtual_mic.monitor
                                    ↓
                    virtual_mic_source
                                    ↓
                    Chrome getUserMedia()
                                    ↓
                    Google Meet WebRTC
                                    ↓
                    Meeting participants hear audio
```

## Troubleshooting

### Problem: VNC Shows Blank/Black Screen

**Possible causes:**
1. Xvfb failed to start
2. x11vnc cannot connect to Xvfb
3. Chrome crashed before rendering

**Diagnosis:**
```bash
# Check Xvfb is running
docker compose exec meeting-bot pgrep -f "Xvfb.*:99"

# Check x11vnc is running
docker compose exec meeting-bot pgrep -f x11vnc

# Check display socket exists
docker compose exec meeting-bot ls -la /tmp/.X11-unix/X99

# Run health check
docker compose exec meeting-bot /app/vnc_healthcheck.sh
```

**Solution:**
```bash
# Restart the container
docker compose restart meeting-bot

# If that fails, rebuild
docker compose build meeting-bot
docker compose up -d meeting-bot
```

### Problem: Cannot Connect to Port 6080

**Possible causes:**
1. websockify crashed
2. Port conflict on host
3. Firewall blocking

**Diagnosis:**
```bash
# Check if port is listening
docker compose exec meeting-bot netstat -tuln | grep 6080

# Check websockify process
docker compose exec meeting-bot pgrep -f websockify

# Check container logs
docker logs meeting-bot 2>&1 | grep -i "websock\|novnc"
```

**Solution:**
```bash
# Try alternative port in docker-compose.yml
ports:
  - "6081:6080"  # Use 6081 instead
```

### Problem: Chrome Shows "Aw, Snap!" or Crashes

**Possible causes:**
1. Insufficient shared memory
2. Missing Chrome dependencies
3. Chrome profile corruption

**Diagnosis:**
```bash
# Check shared memory size
docker inspect meeting-bot | grep -i shm

# Check Chrome logs
docker compose exec meeting-bot cat /app/recordings/*.log

# Check for lock files
docker compose exec meeting-bot ls -la /app/chrome_profile/Singleton*
```

**Solution:**
```bash
# Increase shared memory (docker-compose.yml)
meeting-bot:
  shm_size: "2gb"

# Clear Chrome profile locks
docker compose exec meeting-bot rm -f /app/chrome_profile/Singleton*

# Restart
docker compose restart meeting-bot
```

### Problem: VNC is Slow/Laggy

**Possible causes:**
1. Network latency
2. High screen update frequency
3. Browser rendering heavy content

**Solutions:**
```bash
# Reduce VNC quality (modify entrypoint.sh x11vnc command)
x11vnc ... -quality 5 -compress level 2

# Use native VNC client instead of browser
# (RealVNC, TightVNC are faster than noVNC)
```

### Problem: No Audio in Meeting

**Possible causes:**
1. PulseAudio not running
2. Virtual sink not created
3. TTS container not connected to socket

**Diagnosis:**
```bash
# Check PulseAudio
docker compose exec meeting-bot pactl info

# Check virtual sink
docker compose exec meeting-bot pactl list sinks short

# Check TTS can connect
docker compose exec tts pactl info
```

**Solution:**
```bash
# Restart PulseAudio
docker compose exec meeting-bot pulseaudio -k
docker compose exec meeting-bot pulseaudio --system --daemonize

# Recreate virtual sink
docker compose exec meeting-bot pactl load-module module-null-sink sink_name=virtual_mic
docker compose exec meeting-bot pactl load-module module-virtual-source source_name=virtual_mic_source master=virtual_mic.monitor
```

## Health Check

A comprehensive health check script is included:

```bash
# Run health check
docker compose exec meeting-bot /app/vnc_healthcheck.sh

# Or manually
docker compose exec meeting-bot bash
/app/vnc_healthcheck.sh
```

**Output includes:**
- Xvfb status (PID, display socket, responsiveness)
- x11vnc status (PID, port 5900)
- noVNC status (PID, port 6080, web server)
- PulseAudio status (server, sinks, sources)
- Chrome/Playwright status
- Environment variables
- Disk space
- Recent logs

## Logs

### Location

All logs are stored in:
```
/app/recordings/entrypoint.log
```

### Access Logs

```bash
# View entrypoint log
docker compose exec meeting-bot cat /app/recordings/entrypoint.log

# Follow logs in real-time
docker logs -f meeting-bot

# Filter for specific components
docker logs meeting-bot 2>&1 | grep -i "xvfb\|vnc\|pulse"
```

### Log Levels

- `[INFO]` — Normal operation
- `[OK]` — Successful component startup
- `[ERROR]` — Component failure (requires attention)

## Advanced Configuration

### Change VNC Resolution

Edit `entrypoint.sh`:
```bash
XVFB_SCREEN="1920x1080x24"  # Full HD
```

### Enable VNC Password

Edit `entrypoint.sh` (x11vnc command):
```bash
x11vnc ... -passwd "your_secure_password"
```

### Custom noVNC Theme

Replace `/opt/novnc/index.html` in the Dockerfile with your custom HTML.

### Multiple VNC Viewers

The `-shared` flag allows multiple simultaneous VNC connections. Useful for:
- Monitoring + debugging
- Screen sharing during development
- Recording sessions

## Security Considerations

### Current Security Posture

- **VNC has NO password** — only accessible from localhost by default
- **Docker network isolation** — only containers on `interview-net` can access
- **Ports exposed to host** — 5900, 6080 are accessible from your machine

### Hardening Options

1. **Enable VNC password:**
   ```bash
   x11vnc -passwd "your_password"
   ```

2. **Restrict host port access:**
   ```yaml
   # docker-compose.yml
   ports:
     - "127.0.0.1:6080:6080"  # localhost only
   ```

3. **Use SSL/TLS:**
   - Configure websockify with certificates
   - Use reverse proxy (nginx, traefik)

4. **Network policies:**
   - Use Docker network segmentation
   - Limit container-to-container access

## Performance Tuning

### Optimize VNC Refresh Rate

Edit `entrypoint.sh` (x11vnc command):
```bash
x11vnc ... -wait 50 -sendptr 5  # Reduce update frequency
```

### Reduce Chrome Memory Usage

Add to Chrome args in `bot.py`:
```python
chrome_args = [
    "--js-flags=--max-old-space-size=512",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    ...
]
```

### Monitor Resource Usage

```bash
# Container stats
docker stats meeting-bot

# Inside container
docker compose exec meeting-bot top
```

## Backup & Restore

### Chrome Profile Backup

The Chrome profile (cookies, sessions) is stored in:
```
/app/chrome_profile
```

**Backup:**
```bash
docker run --rm \
  -v meeting_bot_chrome_profile:/source \
  -v $(pwd)/backup:/backup \
  alpine tar czf /backup/chrome_profile.tar.gz -C /source .
```

**Restore:**
```bash
docker run --rm \
  -v meeting_bot_chrome_profile:/target \
  -v $(pwd)/backup:/backup \
  alpine tar xzf /backup/chrome_profile.tar.gz -C /target
```

## Related Documentation

- [Main README](../README.md) — Project overview
- [TTS Documentation](../../TTS/README.md) — Text-to-speech service
- [Main-Agent Documentation](../../Main-Agent/README.md) — LLM interview agent
- [Docker Compose](../docker-compose.yml) — Full system orchestration

## Support

For issues or questions:
1. Run the health check script first
2. Check logs in `/app/recordings/`
3. Review the troubleshooting section above
4. Check container logs: `docker logs meeting-bot`
