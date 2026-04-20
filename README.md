# AI Interviewer

An autonomous AI system that joins a Google Meet call, conducts a structured technical interview with a candidate, and produces a hire/no-hire evaluation — without a human interviewer in the room.

## How it works

```
Recruiter submits form (UI)
  → MongoDB insert triggers the pipeline
    → Meeting-Bot (headless Chrome) joins the Meet call
    → Candidate speaks → Web Speech API → transcript saved to MongoDB
    → Main-Agent (Gemini LLM) reads transcript → generates response
    → TTS (Edge TTS) synthesizes audio → plays through virtual mic into the call
    → Loop continues across 5 interview phases
    → Final evaluation (HIRE / MAYBE / NO_HIRE) saved to MongoDB
```

## Services

| Service | Role |
|---|---|
| `UI` | FastAPI web form — start interviews, view transcripts, read evaluations |
| `Main-Agent` | LLM pipeline — Gemini 2.0 Flash Lite, 5-phase interview logic, evaluation |
| `Meeting-Bot` (orchestrator) | Watches for new interviews, spawns one `meeting-bot-{id}` container per interview |
| `meeting-bot-{id}` | Headless Chrome + Web Speech API STT + Edge TTS playback per interview |
| `MongoDB` | Event bus (change streams) + persistent storage |
| `Redis` | Echo guard, interrupt signals, speech buffer, session state |
| `Prometheus + Grafana` | LLM latency, circuit breaker, active interview metrics |
| `nginx` | Reverse proxy + TLS termination |
| `Cleanup-Service` | Detects stalled/crashed interviews, marks them abandoned |

## Interview phases

`INTRO` (2–3 turns) → `EXPERIENCE` (3–8) → `TECHNICAL` (4–10) → `BEHAVIORAL` (3–8) → `CLOSING` (3–6)

Phase transitions are driven by the LLM signalling `ready_to_advance` in structured JSON output.

## Quick start

```bash
cp .env.example .env
# Fill in: GEMINI_API_KEY, BOT_EMAIL, BOT_PASSWORD, MEETING_URL pattern
docker compose up --build
# Open http://localhost:8080
```

## Environment variables

All keys live in `.env` (root). Key ones:

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` | Primary LLM provider (free tier works) |
| `DEEPSEEK_API_KEY` | Fallback LLM provider |
| `OPENROUTER_API_KEY` | Second fallback (Qwen model) |
| `BOT_EMAIL` / `BOT_PASSWORD` | Google account the bot uses to join Meet |
| `STT_LANGUAGE` | Web Speech API language code (default: `en-US`) |
| `EDGE_TTS_VOICE` | Bot voice (default: `en-US-AndrewNeural`) |
| `LLM_PROVIDER` | Primary provider: `gemini` / `deepseek` / `qwen` |

## Architecture notes

- **Event bus**: MongoDB change streams only — no Redis pub/sub for pipeline triggers
- **Parallelism**: One Docker container per interview (isolated Chrome + PulseAudio)
- **LLM fallback chain**: Gemini → DeepSeek → Qwen (automatic on failure)
- **STT**: Web Speech API injected into Chrome (not caption scraping)
- **TTS**: Edge TTS → MP3 → mpg123 → PulseAudio virtual mic → Chrome WebRTC

## UI pages

| Route | Description |
|---|---|
| `GET /` | Start a new interview |
| `GET /status/{id}/stream` | Live SSE status feed |
| `GET /conversations` | All past interviews |
| `GET /conversation/{id}` | Full transcript |
| `GET /evaluations` | All evaluations |
| `GET /evaluation/{id}` | Hire/no-hire report |
| `GET /logs` | Live service logs |

## Branch

Active development: `feature/product-v2`
