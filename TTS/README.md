# TTS Service

A real-time, streaming Text-to-Speech microservice for conversational AI meeting bots. Monitors MongoDB for agent utterances, synthesises speech via ElevenLabs, and outputs audio to a PulseAudio virtual microphone — which a headless browser captures and streams into a live meeting as the bot's voice.

---

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Configure
cp .env.example .env
# Edit .env — set ELEVENLABS_API_KEY

# 3. Start MongoDB with replica set (required for Change Streams)
docker run -d --name mongo -p 27017:27017 mongo:7.0 mongod --replSet rs0 --bind_ip_all
sleep 5
docker exec mongo mongosh --eval "rs.initiate({_id:'rs0',members:[{_id:0,host:'localhost:27017'}]})"

# 4. Run the service
uv run python -m src.tts.main

# 5. Trigger synthesis
mongosh interviews --eval "db.transcripts.insertOne({interview_id:'test',speaker:'agent',text:'Hello world.',audio_url:null,timestamp:new Date()})"
```

Full Docker stack: `docker compose build && docker compose up`

---

## Architecture

```
MongoDB (interviews.transcripts)
    ↓ [Change Streams — insert where speaker=agent, audio_url=null]
TranscriptListener → ElevenLabsSynthesizer → AudioPlayer → PulseAudio (virtual_mic)
    ↑                                              ↑
NATS (tts.interrupt) → InterruptHandler ───────────  (<50ms stop latency)
    ↓
TranscriptUpdater → GridFS (audio bucket) → audio_url updated
```

---

## Documentation

| Doc | Description |
|---|---|
| [01 — Overview](docs/01-overview.md) | What this service is, the bigger meeting bot picture |
| [02 — Architecture](docs/02-architecture.md) | Components, data flow, design decisions with rationale |
| [03 — Building from Scratch](docs/03-building-from-scratch.md) | Complete step-by-step setup guide |
| [04 — Components](docs/04-components.md) | Deep dive into each Python module |
| [05 — Configuration](docs/05-configuration.md) | All environment variables with examples |
| [06 — Data Contracts](docs/06-data-contracts.md) | MongoDB schema, NATS protocol, GridFS format |
| [07 — Deployment](docs/07-deployment.md) | Local, Docker, production deployment |
| [08 — Integration](docs/08-integration.md) | Connecting to headless browser and meeting bot |
| [09 — Testing](docs/09-testing.md) | Test suite, benchmark script, play from DB |
| [10 — Troubleshooting](docs/10-troubleshooting.md) | Common issues and fixes |

---

## Key Design Decisions

- **MongoDB Change Streams** — push-based queue, no polling, guaranteed delivery
- **ElevenLabs streaming** — first audio in <300ms, no buffering before playback
- **NATS interrupt** — <50ms stop latency via asyncio.Event + chunk-boundary check
- **PulseAudio virtual mic** — audio enters meeting without a real microphone
- **GridFS caching** — synthesised audio stored for replay and audit

---

## Tech Stack

Python 3.11 · motor · nats-py · httpx · sounddevice · numpy · pydantic
MongoDB 7.0 (replica set) · NATS 2.10 · PulseAudio · Docker · uv

---

## Scripts

```bash
# Measure end-to-end synthesis latency
uv run python scripts/benchmark.py --text "Your text here"

# Play audio already stored in MongoDB GridFS
uv run python scripts/play_from_db.py

# Run all tests
uv run pytest tests/ -v
```
