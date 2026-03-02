# VNC Quick Reference Card

## 🚀 Quick Start

```bash
# Build & Start
cd "D:\PF-24.2\AI Interviwer"
docker compose up --build meeting-bot

# Access VNC
http://localhost:6080/
```

## 🔍 Diagnostics

```bash
# Health Check (run this first!)
docker compose exec meeting-bot /app/vnc_healthcheck.sh

# View logs
docker logs meeting-bot
docker compose exec meeting-bot cat /app/recordings/entrypoint.log

# Check processes
docker compose exec meeting-bot ps aux | grep -E "Xvfb|x11vnc|websockify|pulse"
```

## 🔧 Common Fixes

```bash
# Restart VNC
docker compose restart meeting-bot

# Clear Chrome locks
docker compose exec meeting-bot rm -f /app/chrome_profile/Singleton*

# Rebuild from scratch
docker compose down meeting-bot
docker compose build --no-cache meeting-bot
docker compose up meeting-bot
```

## 📊 Ports & Access

| Service | Port | Access |
|---------|------|--------|
| noVNC (Web) | 6080 | http://localhost:6080/ |
| VNC (Native) | 5900 | VNC client → localhost:5900 |
| Xvfb Display | :99 | Internal to container |

## 🎯 What You Should See

**On VNC Landing Page:**
- ✅ Dark blue gradient background
- ✅ "Meeting Bot" title
- ✅ Status box (Display :99, Ports 5900/6080)
- ✅ "Connect to Desktop" button

**After Connecting:**
- ✅ Dark blue desktop (#1e3a5f)
- ✅ Optional: xterm window with "Meeting Bot - Display :99"
- ✅ When meeting starts: Google Chrome window

## ⚠️ Troubleshooting Flow

```
1. Run health check
   ↓
2. Check logs
   ↓
3. Restart container
   ↓
4. Rebuild if needed
   ↓
5. Check Docker resources (disk, memory)
```

## 📝 Key Files

| File | Purpose |
|------|---------|
| `entrypoint.sh` | VNC startup script |
| `Dockerfile` | Image build config |
| `vnc_healthcheck.sh` | Diagnostics |
| `VNC_README.md` | Full documentation |

## 🔐 Security Notes

- VNC has **no password** (localhost access only by default)
- Ports 5900/6080 are exposed to your machine
- For production: enable password, restrict host ports, use SSL

## 💡 Pro Tips

1. **Auto-connect**: The landing page auto-connects to VNC
2. **Multiple viewers**: Connect multiple VNC clients simultaneously
3. **Scaling**: noVNC scales to fit your browser window
4. **Reconnect**: noVNC auto-reconnects if connection drops
5. **Fullscreen**: Press F8 in noVNC for fullscreen mode

## 🎨 Customization

**Change Resolution:**
Edit `entrypoint.sh`:
```bash
XVFB_SCREEN="1920x1080x24"
```

**Change Desktop Color:**
Edit `entrypoint.sh`:
```bash
xsetroot -display :99 -solid '#2d5a87'
```

**Add VNC Password:**
Edit `entrypoint.sh`:
```bash
x11vnc ... -passwd "your_password"
```

## 📞 When to Use VNC

- ✅ Debugging why Chrome crashed
- ✅ Verifying Google Meet loaded correctly
- ✅ Watching caption scraping in action
- ✅ Confirming microphone/camera permissions
- ✅ Screen recording for bug reports
- ✅ Live monitoring during interviews

## 🚫 When NOT to Use VNC

- ❌ Production interviews (use headless mode)
- ❌ When you only need logs
- ❌ For configuration changes (edit files instead)
- ❌ As a security camera (it's not recording by default)

---

**Full Documentation:** See `VNC_README.md` for complete details.
