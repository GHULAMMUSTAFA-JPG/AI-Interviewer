# AI Interviewer — Complete Project Briefing
**Last updated:** 2026-04-17  
**Active branch:** `feature/product-quality`  
**Status:** Running in Docker, all 7 services healthy

---

## 1. What This Project Is

An automated AI interviewer that joins a Google Meet call as a bot, listens to a candidate speaking, generates AI responses with an LLM, and speaks back using TTS — conducting a full structured job interview without a human interviewer present.

**User flow:**
1. HR/recruiter opens `http://localhost:8080`
2. Fills in: Meeting URL, Job Description, Company Info, Candidate CV
3. Clicks Submit → bot immediately joins the Google Meet
4. Bot introduces itself, asks interview questions, follows up, evaluates, closes
5. Final hire/no-hire evaluation written to MongoDB

---

## 2. Architecture

**Pattern:** Event-driven microservices. MongoDB change streams are the sole event bus. No polling, no REST calls between services.

```
UI (FastAPI, :8080)
  └── POST /start → INSERT interviews.interviews {status:"in_progress", meeting_url, cv, jd, ...}
        │
        ├── [Change stream] → Main-Agent (greeting watcher)
        │     └── INSERT interviews.transcripts {speaker:"agent", text:"Hello! I'm..."}
        │           └── [Change stream] → TTS
        │                 └── Edge TTS → WAV → PulseAudio → VirtualSink → Chrome hears bot speak
        │
        └── [Change stream] → Meeting-Bot
              └── Playwright Chrome joins meeting_url
                    └── JS injected into Google Meet page
                          └── Web Speech API captures candidate audio
                                └── Console events → Python handler
                                      └── INSERT interviews.transcripts {speaker:"candidate", text:"..."}
                                            └── [Change stream] → Main-Agent (candidate watcher)
                                                  └── LLM generates response
                                                        └── INSERT {speaker:"agent", text:"..."}
                                                              └── [Change stream] → TTS → plays → loop
```

**IMPORTANT:** The bot does NOT scrape Google Meet captions. It uses JavaScript injection via Playwright to access the browser's Web Speech API directly from inside the Chrome tab. Console events carry the recognized speech back to Python.

---

## 3. Services (7 total, all Docker containers)

### 3.1 UI (`UI/`)
- **Tech:** FastAPI + Jinja2, Python
- **Port:** 8080 (external)
- **Role:** Web form that fires the entire pipeline with a single MongoDB insert
- **Key file:** `UI/main.py` — `POST /start` creates the interview document
- **Also has:** SSE endpoint `/status/{id}/stream` for live interview status, `/conversations` page

### 3.2 Main-Agent (`Main-Agent/`)
- **Tech:** Python, structlog, motor (async MongoDB), asyncio
- **LLM:** `gemini-2.0-flash-lite` (Gemini API) — **do not change**
- **Role:** LLM brain. Two change stream watchers:
  - **Greeting watcher:** fires on new `interviews.interviews` insert → sends opening message
  - **Candidate watcher:** fires on `speaker=candidate` transcript insert → generates response
- **Key files:**
  - `Main-Agent/src/main.py` — entry point, change stream setup
  - `Main-Agent/src/agent/pipeline.py` — main LLM call chain
  - `Main-Agent/src/agent/phases.py` — interview phase state machine
  - `Main-Agent/src/agent/prompt_builder.py` — builds prompts from CV/JD/phase
  - `Main-Agent/src/agent/evaluator.py` — final hire/no-hire evaluation
  - `Main-Agent/src/agent/context_loader.py` — pre-warms CV/JD context
  - `Main-Agent/src/agent/summary_generator.py` — conversation summary
  - `Main-Agent/src/agent/llm_provider.py` — Gemini API wrapper with circuit breaker
- **Interview phases:** INTRO → TECHNICAL → BEHAVIOURAL → CLOSING → EVALUATION
- **Circuit breaker:** opens after repeated LLM failures, sends fallback responses
- **Per-interview asyncio.Lock:** prevents duplicate LLM calls for same interview

### 3.3 TTS (`TTS/`)
- **Tech:** Python, asyncio, motor, sounddevice/subprocess
- **Role:** Watches `speaker=agent + audio_url=null` transcripts → synthesizes speech → plays through PulseAudio → candidate hears bot
- **Active synthesizer:** Edge TTS (`TTS/src/tts/edge_synthesizer.py`) — Microsoft's free cloud TTS
- **Inactive synthesizer:** ElevenLabs (`TTS/src/tts/elevenlabs_synthesizer.py`) — file exists, NOT used
- **Audio path:** Edge TTS → WAV file → PulseAudio `VirtualSink` → Chrome virtual mic input → Google Meet
- **Key files:**
  - `TTS/src/tts/main.py` — change stream watcher + dispatcher
  - `TTS/src/tts/tts_queue.py` — per-interview queue with 1s backpressure
  - `TTS/src/tts/audio_player.py` — plays WAV through paplay
  - `TTS/src/tts/interrupt_handler.py` — stops playback when candidate interrupts
  - `TTS/src/tts/status_updater.py` — writes tts status to Redis

### 3.4 Meeting-Bot (`Meeting-Bot/`)
- **Tech:** Python, Playwright (channel="chrome"), Google Chrome headless, PulseAudio, Xvfb
- **Role:** Joins Google Meet, injects JS for speech recognition, feeds candidate words to MongoDB
- **STT method:** JavaScript injection into the Chrome tab (Web Speech API), NOT caption scraping
- **Key files:**
  - `Meeting-Bot/main.py` — entry point, starts watchers
  - `Meeting-Bot/meeting_watcher.py` — MongoDB change stream → fires `join_meeting()`
  - `Meeting-Bot/join_meeting.py` — full session lifecycle (join → interview → leave)
  - `Meeting-Bot/stt_injector.py` — injects Web Speech API JS into Google Meet page
  - `Meeting-Bot/stt_console_handler.py` — receives speech events from browser console
  - `Meeting-Bot/stt_speech_buffer.py` — buffers/debounces words into sentences, flush to MongoDB
  - `Meeting-Bot/stt_audio_router.py` — manages PulseAudio sinks/sources, routes Chrome audio
  - `Meeting-Bot/stt_echo_guard.py` — prevents bot from hearing its own TTS speech
  - `Meeting-Bot/mongo_handler.py` — writes transcript inserts with 3x retry backoff
  - `Meeting-Bot/state_manager.py` — Redis state (status, heartbeat)
  - `Meeting-Bot/media_controls.py` — mic/camera controls in Meet UI
  - `Meeting-Bot/entrypoint.sh` — starts Xvfb + PulseAudio + Chrome policy + Python
- **Audio architecture inside container:**
  - `VirtualSink` — TTS audio plays into this
  - `VirtualSink.monitor` → remapped as `BotMic` → Chrome mic input (candidate hears bot)
  - `virtual_mic_source` → Web Speech API reads from this (STT)
  - Silence keep-alive: `paplay /dev/zero` keeps PulseAudio from sleeping

### 3.5 Redis (`redis:7-alpine`)
- **Role:** Real-time state, echo guard coordination, speech buffer, heartbeat monitoring
- **Key namespaces:**
  - `agent:{interview_id}:status` — LLM processing state
  - `tts:{interview_id}:status` — TTS playback state (used by echo guard)
  - `bot:{interview_id}:heartbeat` — meeting-bot liveness (10s TTL)
  - `speech:{interview_id}:buffer` — accumulated candidate words before flush
  - `interrupt:{interview_id}` — interrupt signal from candidate to stop TTS
- **Config:** `docker/redis/redis.conf` — no auth currently (open issue)

### 3.6 MongoDB (`mongo:7.0`)
- **Mode:** Single-node replica set (`rs0`) — required for change streams
- **Port:** 27017 (external, also accessible via MongoDB Compass)
- **Collections:**
  - `interviews.interviews` — interview metadata, phase, status
  - `interviews.transcripts` — all messages (agent + candidate)
  - `interviews.evaluations` — final hire/no-hire recommendation
  - `interviews.agent_state` — LLM resume token for crash recovery

### 3.7 Cleanup-Service
- **Role:** Detects stale/crashed interviews (no heartbeat > 90s), marks them abandoned
- **Location:** runs as a separate process within the meeting-bot container or standalone

---

## 4. Docker Infrastructure

- **Compose file:** `docker-compose.yml` (root)
- **Dockerfiles:** `docker/` folder
  - `docker/meeting-bot.Dockerfile` — Ubuntu 22.04, Chrome, PulseAudio, Xvfb, Python 3.11
  - `docker/main-agent.Dockerfile`
  - `docker/tts.Dockerfile`
  - `docker/ui.Dockerfile`
- **Network:** `interview-net` (internal Docker bridge, all services)
- **Volumes:** `redis_data`, `mongodb_data`, `/app/recordings`, `/app/chrome_profile`, `/var/run/pulse`
- **Mirror:** `azure.archive.ubuntu.com` used in meeting-bot Dockerfile (avoids Canonical CDN 400 errors)
- **VNC:** Completely removed (was debug-only, no longer in Dockerfile or compose)
- **Start:** `docker compose up --build` from repo root
- **Secrets:** `.env` file (copy from `.env.example`), keys: `GEMINI_API_KEY`, `ELEVENLABS_API_KEY` (unused), `BOT_EMAIL`

---

## 5. Git Branches

### `master`
- Baseline from very early development. Only contains initial setup commits. Nothing useful here — all real work is on `feature/product-quality`.

### `feature/product-quality` ← ACTIVE BRANCH, all current work
The main working branch. Contains everything from initial bot implementation through the current production-quality system. Key milestones committed here:
- Redis integration across all services
- MongoDB replica set setup
- Edge TTS integration (replaced ElevenLabs and Piper)
- Full PulseAudio audio routing (BotMic / VirtualSink)
- Echo guard (prevents bot from hearing itself)
- Interruption system
- Phase management (INTRO/TECH/BEHAVIOURAL/CLOSING/EVAL)
- Context pre-warming for faster LLM first response
- Circuit breaker for LLM failures
- Crash recovery (resume token, TTS backfill)
- Heartbeat + cleanup service
- UI live status view (SSE)
- All P0/P1/P2/P3 product quality improvements (Apr 2026)

### Feature branches (all forked from `feature/product-quality`, NOT merged back)
Each has 1 focused commit ahead of the branch point, then shares the full history below:

| Branch | What it added |
|---|---|
| `feature/audio-routing` | PulseAudio re-routing after PA crash/restart, poll VirtualSink readiness before routing |
| `feature/data-integrity` | asyncio.Queue fallback — if MongoDB transcript insert fails, queues it for retry without dropping |
| `feature/echo-guard` | Conditional buffer delay, `safe_evaluate` sync, `botSpeakingCount` JS counter to track overlapping speech |
| `feature/edge-tts` | Edge TTS synthesizer integration (Microsoft cloud TTS, free, no API key) |
| `feature/interruption-system` | Redis-first optimistic interrupt — reduced interrupt latency from 2000ms to ~50ms |
| `feature/llm-pipeline` | Isolated summary circuit breaker, async evaluation, circuit breaker visibility in logs, Qwen model regex fix |
| `feature/meeting-bot-orchestration` | Task cancellation timeout on bot shutdown, safe navigation handler, `wait_for` admission guard |
| `feature/mutation-observer-interrupt` | MutationObserver JS approach for detecting candidate speech (early experiment) |
| `feature/observability` | `turn_id` tracing on every log line, Redis metrics endpoint, circuit breaker state visible in logs |
| `feature/phase-management` | CLOSING phase tuning: `min_turns` 2→3, `max_turns` 5→6 |
| `feature/piper-tts` | Piper TTS (offline, free) — explored but replaced by Edge TTS |
| `feature/stt-pipeline` | Phase-aware silence thresholds via `__set_silence_ms`, 20s STT stall detection |
| `feature/tts-pipeline` | 1s backpressure between queued agent messages (prevents TTS flooding) |
| `fix/audio-stt-tts-pipeline` | Audio pipeline hotfixes — backoff reset, echo gate tuning, console flush |
| `fix/caption-speaker-detection-and-interrupt-timing` | Old work: caption-based speaker detection (now abandoned — not using captions) |
| `improvements/timing-and-status` | Caption dedup, interrupt timing, latency optimizations (early timing work) |
| `speech-api` | Speech API experiments — early exploration of Web Speech API approach |

---

## 6. What IS and IS NOT Implemented

### Implemented and Working
- Full end-to-end interview loop (join → greet → Q&A → evaluate → leave)
- JavaScript-injected Web Speech API for candidate STT (NOT caption scraping)
- Edge TTS → PulseAudio → Chrome audio routing for bot voice
- Echo guard: bot does not transcribe its own TTS output
- Interruption: candidate can interrupt bot mid-sentence (~50ms detection via Redis)
- Phase-aware interview: 5 phases with configurable turn limits
- Circuit breaker on LLM calls with fallback responses
- TTS backpressure: queued messages play with 1s gap
- Crash recovery: Main-Agent resume token, TTS 5-min backfill on restart
- Heartbeat monitoring + cleanup service (marks stale interviews)
- MongoDB data integrity: 3x retry with backoff on transcript inserts
- asyncio.Queue fallback for failed inserts
- Redis state: real-time status visible in UI via SSE
- Per-interview asyncio.Lock (no duplicate LLM calls)
- Context pre-warming: CV/JD loaded before first LLM call
- PulseAudio health monitor + auto-restart on crash
- Chrome profile persistence across bot restarts
- Structured logging with `interview_id`, `request_id`, `phase`, `turn` (Main-Agent only)

### NOT Implemented (Future Work)
- **Logging:** TTS and Meeting-Bot logs have no `interview_id` tagging, no centralized log storage
- **Monitoring:** No Prometheus metrics, no Grafana, no alerting
- **Security:** MongoDB and Redis have no auth; no nginx TLS; UI has no auth
- **Parallel interviews:** Chrome profile lock + PulseAudio global state prevent >1 concurrent interview
- **Consecutive interview PulseAudio cleanup:** Stale Chrome sink-inputs can cause audio routing issues between back-to-back interviews
- **Kubernetes:** No K8s manifests; not K8s-ready (PulseAudio + Chrome per-pod design needed)
- **Meeting-Bot crash alert:** No Slack/webhook notification when bot crashes mid-interview
- **Redis shadow buffer:** If Redis crashes mid-sentence, in-flight candidate words are lost
- **ElevenLabs streaming:** File exists (`elevenlabs_synthesizer.py`) but inactive — Edge TTS is used

---

## 7. Known Issues / Bugs

| Issue | Status | Location |
|---|---|---|
| `reset_session()` called with no args (old Docker image crash) | **Fixed** — new image built and deployed | `Meeting-Bot/join_meeting.py` was fixed in commits, Docker rebuilt Apr 2026 |
| PulseAudio does not cleanly release Chrome sink-inputs on interview exit | **Open** — affects consecutive interviews | `Meeting-Bot/join_meeting.py` exit sequence |
| Chrome profile lock `/app/chrome_profile/SingletonLock` | **Open** — prevents parallel interviews | `Meeting-Bot/join_meeting.py:113` needs per-interview path |
| `pactl` calls not serialized | **Open** — race condition with parallel interviews | `Meeting-Bot/stt_audio_router.py` |
| No MongoDB index on `transcripts` | **Open** — becomes slow at scale | `Main-Agent/src/main.py` startup needs index creation |

---

## 8. Performance Profile

| Metric | Current value |
|---|---|
| VAD silence window | 500–800ms |
| TTS restart delay after bot speaks | 500ms |
| Interrupt detection latency | ~50ms (Redis-first) |
| End-to-end response (INTRO, fast path) | ~1300–1800ms |
| LLM model | gemini-2.0-flash-lite |
| TTS engine | Edge TTS (Microsoft, free) |

---

## 9. Environment Variables (`.env`)

```
GEMINI_API_KEY=...          # Required — LLM
ELEVENLABS_API_KEY=...      # Present but unused (Edge TTS is active)
BOT_EMAIL=...               # Google account for bot to log into Meet
MONGODB_URI=...             # Auto-overridden in Docker: mongodb://mongodb:27017
REDIS_URL=...               # Auto-overridden in Docker: redis://redis:6379/0
```

---

## 10. How to Run

```bash
# From repo root:
cp .env.example .env          # fill in GEMINI_API_KEY, BOT_EMAIL
docker compose up --build     # builds all images + starts all 7 containers
# Open http://localhost:8080
```

**Check health:**
```bash
docker compose ps             # all should show (healthy)
docker compose logs -f meeting-bot   # watch bot join a meeting
```

---

## 11. Future Roadmap (Priority Order)

**Phase 1 — Security (config only)**
- MongoDB SCRAM auth, Redis requirepass, UI API key header, nginx + TLS

**Phase 2 — Observability**
- Structured JSON logs with `interview_id` in TTS and Meeting-Bot
- Prometheus metrics on all services, Grafana dashboard, degraded-state alerts

**Phase 3 — Reliability**
- PulseAudio cleanup on Meeting-Bot exit (consecutive interview fix)
- In-memory shadow buffer for Redis speech keys
- Meeting-Bot crash alert → Slack/webhook

**Phase 4 — Performance**
- MongoDB compound index on `transcripts` (interview_id + speaker)
- Redis pub/sub pipeline triggers (bypass MongoDB change stream, save ~100–380ms)

**Phase 5 — Scale**
- Per-interview Chrome profile path (1-line fix enables parallel interviews)
- `asyncio.Lock` on all `pactl` calls (serializes audio ops for parallel safety)

**Phase 6 — Kubernetes**
- K8s manifests for stateless services (Main-Agent, TTS, UI)
- MongoDB Atlas + managed Redis
- Meeting-Bot as StatefulSet with PulseAudio sidecar
