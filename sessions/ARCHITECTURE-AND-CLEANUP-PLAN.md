# AI Interviewer System — Architecture & Cleanup Plan

**Document Date:** March 3, 2026  
**Status:** Draft — Pending Execution  
**Purpose:** Reorganize codebase for maintainability, clarity, and production readiness

---

## Executive Summary

This document provides:
1. **Current State Analysis** — What exists, what works, what's broken
2. **Target Architecture** — Clean separation of concerns, standardized patterns
3. **Cleanup Plan** — Step-by-step reorganization with minimal disruption

### Key Issues Identified

| Issue | Severity | Impact |
|-------|----------|--------|
| STT service missing (only Meeting-Bot exists) | High | System incomplete |
| TTS `models/` directory was missing (partially fixed) | Medium | Build failures |
| NATS dependency in TTS but not implemented | Medium | Confusion, dead code |
| VNC/noVNC not working properly | Low | Debugging difficulty |
| Inconsistent Python versions (3.11, 3.12, 3.13) | Low | Maintenance overhead |
| Gemini API rate limits (free tier exhausted) | High | System unusable |
| CLAUDE.md files are generic templates, not project-specific | Low | Poor AI assistance |
| No `.specify/` directory structure (PHR/ADR missing) | Low | No development history |

---

## Part 1: Current Architecture (As-Is)

### 1.1 System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         AI Interviewer System                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐   │
│  │  UI  │────▶│  Main-Agent  │────▶│     TTS      │────▶│ Meeting-Bot  │   │
│  │ :8080│     │   (LLM Brain)│     │ (ElevenLabs) │     │   (Chrome)   │   │
│  └──────┘     └──────┬───────┘     └──────────────┘     └──────┬───────┘   │
│       │              │                                         │           │
│       │              │              ┌──────────────┐           │           │
│       │              └─────────────▶│  MongoDB     │◀──────────┘           │
│       │                             │  Change Stream│                      │
│       │                             └──────────────┘                       │
│       │                                                                      │
│       └────────────────────────────────────────────────────────────────────▶│
│                                    (Event Bus)                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Component Inventory

#### **Root Level (`D:\PF-24.2\AI Interviwer\`)**

| File/Directory | Purpose | Status |
|----------------|---------|--------|
| `docker-compose.yml` | Orchestrates all services | ✅ Working |
| `.env` / `.env.example` | Environment configuration | ✅ Working |
| `info.md` | System description | ✅ Reference |
| `report.md` | Unknown | ⚠️ Review needed |
| `.md` | Empty/unknown | ❌ Delete |
| `sessions/` | Unknown | ⚚ Review needed |
| `*.txt` session logs | Previous AI session exports | ⚚ Archive/Delete |

#### **Main-Agent (`./Main-Agent/`)**

| File/Directory | Purpose | Status |
|----------------|---------|--------|
| `src/main.py` | Entry point — MongoDB change stream listener | ✅ Working |
| `src/config.py` | Configuration + MongoDB connection | ✅ Working |
| `src/exceptions.py` | Custom exceptions | ✅ Working |
| `src/agent/llm_provider.py` | Multi-provider LLM abstraction | ✅ Working (Gemini, OpenAI, Anthropic) |
| `src/agent/pipeline.py` | 5-stage interview pipeline | ✅ Working |
| `src/agent/prompt_builder.py` | Prompt construction (~1600 tokens) | ✅ Working |
| `src/agent/models.py` | Pydantic models | ✅ Working |
| `src/agent/retry.py` | Circuit breaker + retry logic | ✅ Working |
| `src/agent/summary_generator.py` | Conversation summarization | ⚚ Untested |
| `src/agent/evaluator.py` | Post-interview evaluation | ⚚ Untested |
| `src/agent/phases.py` | Phase management | ⚚ Untested |
| `src/agent/validator.py` | Output validation | ⚚ Untested |
| `src/agent/safeguards.py` | Token/duration limits | ⚚ Untested |
| `src/agent/context_loader_optimized.py` | Cached context loading | ✅ Working |
| `src/cache/interview_cache.py` | TTL cache for contexts | ✅ Working |
| `tests/` | Test suite | ⚚ Unknown coverage |
| `setup_db.py` | Database seeding | ✅ Working |
| `clear_db.py` | Database cleanup | ✅ Working |
| `run_demo.py` | Demo runner | ⚚ Untested |
| `pyproject.toml` / `uv.lock` | Python project config | ✅ Working |
| `requirements.txt` | Dependencies | ✅ Working |
| `CLAUDE.md` | Generic template (not project-specific) | ❌ Replace |
| `Dockerfile` | Container image | ✅ Working |

**Issues:**
- Missing Groq provider (user needs high rate limits)
- Generic CLAUDE.md template
- No `.specify/` directory for PHR/ADR

#### **Meeting-Bot (`./Meeting-Bot/`)**

| File/Directory | Purpose | Status |
|----------------|---------|--------|
| `main.py` | Entry point — watches interviews.interviews | ✅ Working |
| `bot.py` | Playwright browser logic | ✅ Working |
| `caption_scraper.py` | DOM caption extraction | ✅ Working |
| `mongo_handler.py` | MongoDB writes | ✅ Working |
| `llm_handler.py` | Post-meeting summary (Gemini) | ⚚ Unused |
| `logger.py` | Async logger | ✅ Working |
| `entrypoint.sh` | Xvfb + PulseAudio startup | ⚠️ VNC issues reported |
| `vnc_healthcheck.sh` | VNC health check | ⚠️ Not working |
| `novnc_index.html` | Custom noVNC landing page | ✅ Present |
| `VNC_*.md` | VNC documentation | ✅ Reference |
| `requirements.txt` | Dependencies | ✅ Working |
| `Dockerfile` | Complex image with VNC/PulseAudio | ⚠️ VNC broken |
| `.env.example` | Environment template | ✅ Working |

**Issues:**
- VNC/noVNC not working (grey/black screen, Connect button fails)
- Complex Dockerfile (200+ lines)
- Multiple VNC documentation files (consolidate needed)

#### **TTS (`./TTS/`)**

| File/Directory | Purpose | Status |
|----------------|---------|--------|
| `src/tts/main.py` | Entry point — TTSService orchestrator | ✅ Working |
| `src/tts/config.py` | Configuration | ✅ Working |
| `src/tts/tts_queue.py` | TranscriptListener (MongoDB change stream) | ✅ Working |
| `src/tts/synthesizer.py` | ElevenLabs integration | ✅ Working |
| `src/tts/audio_player.py` | PCM playback via pacat | ✅ Working |
| `src/tts/interrupt_handler.py` | MongoDB-based interrupt (NATS removed) | ✅ Working (recent change) |
| `src/tts/status_updater.py` | Marks transcripts as played | ✅ Working |
| `src/tts/logging_config.py` | Logging setup | ✅ Working |
| `models/tts_queue.py` | Pydantic models | ✅ Created (was missing) |
| `scripts/` | Utility scripts | ⚚ Review needed |
| `tests/` | Test suite | ⚚ Unknown coverage |
| `pyproject.toml` / `uv.lock` | Python project config | ✅ Working |
| `requirements.txt` | Dependencies | ⚠️ Still lists `nats-py` (removed) |
| `CLAUDE.md` | Generic template | ❌ Replace |
| `Dockerfile` | Container image | ✅ Working |
| `Production-level-readiness.md` | Readiness checklist | ✅ Reference |
| `README.md` | Service documentation | ✅ Reference |
| `*.txt` session logs | Previous AI session exports | ⚚ Archive/Delete |

**Issues:**
- `requirements.txt` still has `nats-py` (should be removed)
- Generic CLAUDE.md template
- No `.specify/` directory

#### **UI (`./UI/`)**

| File/Directory | Purpose | Status |
|----------------|---------|--------|
| `main.py` | FastAPI app (form + POST handler) | ✅ Working |
| `templates/index.html` | Interview creation form | ✅ Working |
| `templates/conversation.html` | Unknown | ⚚ Review needed |
| `templates/conversations.html` | Unknown | ⚚ Review needed |
| `static/style.css` | Basic styles | ✅ Working |
| `requirements.txt` | Dependencies | ✅ Working |
| `CLAUDE.md` | Generic template | ❌ Replace |
| `Dockerfile` | Container image | ✅ Working |
| `.env.example` | Environment template | ✅ Working |

**Issues:**
- Unused templates (`conversation.html`, `conversations.html`)
- Generic CLAUDE.md template

#### **STT (`./STT/`)**

| Status |
|--------|
| ❌ **DIRECTORY DOES NOT EXIST** |

**Note:** STT functionality is currently embedded in Meeting-Bot (caption scraping from Google Meet DOM). This is intentional — Google Meet provides captions natively, so a separate STT service is not needed.

**Recommendation:** Either:
1. Create STT directory with documentation explaining it's not needed (Meet captions are used)
2. Or document clearly that "STT = Meeting-Bot caption scraper"

---

### 1.3 Data Flow (Current)

```
1. User submits form (UI :8080)
   └─▶ INSERT interviews.interviews {status: "in_progress", meeting_url, cv, jd, company}

2. Main-Agent (greeting watcher) detects insert
   └─▶ INSERT interviews.transcripts {speaker: "agent", text: "Hello, welcome..."}

3. TTS detects new agent transcript
   └─▶ ElevenLabs TTS → PulseAudio → Chrome (Meeting-Bot plays audio)

4. Meeting-Bot detects insert (status: "in_progress")
   └─▶ Playwright launches Chrome → joins Google Meet

5. Candidate speaks → Google Meet captions appear in DOM
   └─▶ Meeting-Bot scrapes caption → INSERT interviews.transcripts {speaker: "candidate"}

6. Main-Agent (candidate watcher) detects candidate transcript
   └─▶ Load context → Build prompt (~1600 tokens) → Call LLM → Generate response
   └─▶ INSERT interviews.transcripts {speaker: "agent", text: "<LLM response>"}

7. TTS detects new agent transcript → Loop back to step 3

8. Meeting-Bot (leave watcher) polls interviews.interviews every 5s
   └─▶ If status becomes "completed" or "abandoned" → Click "Leave call" → Exit
```

### 1.4 MongoDB Schema (Current)

#### `interviews.interviews`
```javascript
{
  _id: ObjectId,
  interview_id: UUID,           // Unique identifier (all services use this)
  phase: "INTRO" | "EXPERIENCE" | "TECHNICAL" | "BEHAVIORAL" | "CLOSING",
  turn_count: Number,
  phase_turn_count: Number,
  status: "scheduled" | "in_progress" | "completed" | "abandoned",
  meeting_url: String,
  job_description: String,
  company_info: String,
  candidate_cv: String,
  candidate_name: String,       // Added for prompt personalization
  conversation_summary: String, // Updated every 5 turns
  bot_status: "admitted" | null, // Set by Meeting-Bot when joined
  bot_speaking: Boolean,        // Set by TTS before playing audio
  tts_interrupt: Boolean,       // Set by Meeting-Bot when candidate interrupts
  started_at: Date,
  ended_at: Date,
  evaluation: {                 // Populated on interview completion
    recommendation: String,
    score: Number,
    // ...
  },
  limits: {                     // Safeguard tracking
    forced_end_reason: String
  },
  created_at: Date,
  updated_at: Date
}
```

#### `interviews.transcripts`
```javascript
{
  _id: ObjectId,
  interview_id: UUID,
  speaker: "agent" | "candidate",
  text: String,
  audio_url: String | null,     // Populated by TTS (GridFS path)
  timestamp: Date,
  metadata: {
    phase: String,
    turn_count: Number,
    is_fallback: Boolean,
    llm_provider: String,
    estimated_tokens: Number,
    forced_end: Boolean
  }
}
```

#### `interviews.agent_state`
```javascript
{
  _id: "resume_token",
  token: ObjectId               // MongoDB change stream resume token
}
```

---

## Part 2: Target Architecture (To-Be)

### 2.1 Design Principles

1. **One Component = One Directory** — Each service is self-contained
2. **Standardized Structure** — All services follow the same pattern
3. **Clear Ownership** — Each file has a single, documented purpose
4. **Production Ready** — Logging, monitoring, error handling everywhere
5. **Developer Friendly** — Easy to understand, debug, extend

### 2.2 Proposed Directory Structure

```
D:\PF-24.2\AI Interviwer\
│
├── docker-compose.yml              # Root orchestration (unchanged)
├── .env                            # Root .env (shared secrets)
├── .env.example                    # Template (document all vars)
├── README.md                       # System overview + quickstart
├── ARCHITECTURE.md                 # This document (split into 2 files)
├── .gitignore
│
├── .specify/                       # SpecKit Plus (development history)
│   ├── memory/constitution.md      # Project principles
│   ├── templates/
│   ├── scripts/
│   └── commands/
│
├── history/                        # Prompt History Records + ADRs
│   ├── prompts/
│   │   ├── constitution/
│   │   ├── main-agent/
│   │   ├── meeting-bot/
│   │   ├── tts/
│   │   ├── ui/
│   │   └── general/
│   └── adr/
│       ├── 0001-mongodb-event-bus.md
│       ├── 0002-no-separate-stt.md
│       └── ...
│
├── services/                       # Rename from root-level folders
│   │
│   ├── main-agent/                 # LLM Brain (unchanged structure)
│   │   ├── src/
│   │   ├── tests/
│   │   ├── requirements.txt
│   │   ├── pyproject.toml
│   │   ├── Dockerfile
│   │   ├── README.md               # Service-specific docs
│   │   └── CLAUDE.md               # Project-specific context
│   │
│   ├── meeting-bot/                # Chrome automation
│   │   ├── src/                    # Move all .py files here
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── bot.py
│   │   │   ├── caption_scraper.py
│   │   │   ├── mongo_handler.py
│   │   │   ├── logger.py
│   │   │   └── llm_handler.py      # Mark as unused/deprecated
│   │   ├── tests/
│   │   ├── requirements.txt
│   │   ├── Dockerfile
│   │   ├── entrypoint.sh
│   │   ├── README.md
│   │   └── CLAUDE.md
│   │
│   ├── tts/                        # Text-to-Speech
│   │   ├── src/tts/
│   │   ├── models/
│   │   ├── tests/
│   │   ├── scripts/
│   │   ├── requirements.txt
│   │   ├── pyproject.toml
│   │   ├── Dockerfile
│   │   ├── README.md
│   │   └── CLAUDE.md
│   │
│   └── ui/                         # Web Form
│       ├── src/                    # Move main.py here
│       │   ├── __init__.py
│       │   ├── main.py
│       │   └── templates/
│       ├── static/
│       ├── tests/
│       ├── requirements.txt
│       ├── Dockerfile
│       ├── README.md
│       └── CLAUDE.md
│
├── shared/                         # New: Shared libraries
│   ├── python/
│   │   ├── interview_sdk/          # Common Python SDK
│   │   │   ├── __init__.py
│   │   │   ├── config.py           # Shared config loader
│   │   │   ├── logging.py          # Shared logging config
│   │   │   ├── models.py           # Shared Pydantic models
│   │   │   └── mongodb.py          # Shared MongoDB connection
│   │   └── setup.py
│   └── docs/
│       ├── api-reference.md
│       ├── deployment-guide.md
│       └── troubleshooting.md
│
├── docs/                           # Documentation
│   ├── architecture/
│   │   ├── overview.md
│   │   ├── data-flow.md
│   │   ├── mongodb-schema.md
│   │   └── decisions/              # ADRs (symlinks or copies)
│   ├── services/
│   │   ├── main-agent.md
│   │   ├── meeting-bot.md
│   │   ├── tts.md
│   │   └── ui.md
│   ├── operations/
│   │   ├── deployment.md
│   │   ├── monitoring.md
│   │   ├── backup-restore.md
│   │   └── troubleshooting.md
│   └── development/
│       ├── getting-started.md
│       ├── testing.md
│       └── contributing.md
│
├── scripts/                        # Root-level automation
│   ├── dev/
│   │   ├── start.sh                # docker compose up --build
│   │   ├── stop.sh
│   │   ├── reset-db.sh
│   │   └── logs.sh
│   ├── prod/
│   │   ├── deploy.sh
│   │   └── backup.sh
│   └── tests/
│       └── integration-tests.sh
│
└── tests/                          # Root-level integration tests
    ├── e2e/
    │   └── interview-flow.test.py
    ├── services/
    └── fixtures/
```

### 2.3 Key Changes Summary

| Change | Impact | Effort |
|--------|--------|--------|
| Create `services/` directory, move components | High (organization) | Medium |
| Create `shared/python/interview_sdk/` | High (code reuse) | Medium |
| Create `docs/` structure | Medium (documentation) | Low |
| Create `.specify/` and `history/` | Low (process) | Low |
| Standardize `CLAUDE.md` per service | Medium (AI assistance) | Low |
| Fix VNC/noVNC in Meeting-Bot | High (debugging) | High |
| Add Groq provider to Main-Agent | High (rate limits) | Low |
| Clean up TTS requirements (remove NATS) | Low | Low |
| Remove unused UI templates | Low | Low |
| Document STT decision (not needed) | Low | Low |

---

## Part 3: Cleanup Plan (Execution)

### Phase 1: Immediate Fixes (Critical)

**Goal:** Make system usable again

#### 1.1 Add Groq Provider (Main-Agent)
**Files to modify:**
- `Main-Agent/src/agent/llm_provider.py` — Add `GroqProvider` class
- `Main-Agent/src/config.py` — Add `GROQ_API_KEY`, `GROQ_MODEL`
- `Main-Agent/requirements.txt` — Add `groq>=0.4.0`
- `.env` — Update:
  ```env
  LLM_PROVIDER=groq
  GROQ_API_KEY=gsk_...
  GROQ_MODEL=llama-3.1-70b-versatile
  ```

**Acceptance Criteria:**
- [ ] Groq provider implemented with same retry/circuit breaker as Gemini
- [ ] System switches to Groq without errors
- [ ] Rate limits: 30 RPM free tier confirmed working

#### 1.2 Clean TTS Dependencies
**Files to modify:**
- `TTS/requirements.txt` — Remove `nats-py>=2.6.0`

**Acceptance Criteria:**
- [ ] `nats-py` removed from requirements
- [ ] TTS builds and runs without NATS errors

#### 1.3 Delete Junk Files
**Files to delete:**
- `*.md` (empty file at root)
- `*.txt` session logs (or move to `history/archive/`)
- `TTS/*.txt` session logs

---

### Phase 2: Reorganization (Medium Priority)

**Goal:** Clean, standardized structure

#### 2.1 Create `services/` Directory
**Steps:**
1. Create `services/` directory
2. Move folders:
   ```
   Main-Agent  →  services/main-agent/
   Meeting-Bot  →  services/meeting-bot/
   TTS          →  services/tts/
   UI           →  services/ui/
   ```
3. Update `docker-compose.yml` contexts:
   ```yaml
   interview-agent:
     build:
       context: ./services/main-agent
   meeting-bot:
     build:
       context: ./services/meeting-bot
   tts:
     build:
       context: ./services/tts
   ui:
     build:
       context: ./services/ui
   ```

**Acceptance Criteria:**
- [ ] All services build and run from new locations
- [ ] `docker compose up --build` works without errors

#### 2.2 Standardize Service Structure
**For each service, ensure:**
```
services/<service-name>/
├── src/                    # All Python source files
│   └── <service>/          # Package directory
│       ├── __init__.py
│       ├── main.py         # Entry point
│       └── ...
├── tests/                  # Test suite
├── requirements.txt        # Or pyproject.toml
├── Dockerfile
├── README.md               # Service-specific docs
└── CLAUDE.md               # Project-specific AI context
```

**Acceptance Criteria:**
- [ ] All 4 services follow same structure
- [ ] All README.md files explain: purpose, files, env vars, how to run
- [ ] All CLAUDE.md files explain: service purpose, key files, MongoDB schema, do-not-modify list

#### 2.3 Create Shared SDK
**New directory:** `shared/python/interview_sdk/`

**Modules:**
- `config.py` — Shared config loader (`.env` parsing, validation)
- `logging.py` — Standardized logging config
- `models.py` — Shared Pydantic models (InterviewContext, Transcript, etc.)
- `mongodb.py` — MongoDB connection helper

**Acceptance Criteria:**
- [ ] SDK installable via `pip install -e shared/python/`
- [ ] All services use SDK for config, logging, MongoDB connection
- [ ] Code duplication reduced by >50%

---

### Phase 3: Documentation (Medium Priority)

#### 3.1 Create `.specify/` Structure
**Files to create:**
- `.specify/memory/constitution.md` — Project principles
- `.specify/templates/phr-template.prompt.md` — PHR template
- `.specify/scripts/bash/create-phr.sh` — PHR creation script

**Acceptance Criteria:**
- [ ] Constitution defines code quality, testing, security standards
- [ ] PHR template works for all stages (spec, plan, tasks, red, green, refactor)

#### 3.2 Create `docs/` Structure
**Directories:**
- `docs/architecture/` — System design docs
- `docs/services/` — Per-service documentation
- `docs/operations/` — Deployment, monitoring, troubleshooting
- `docs/development/` — Getting started, testing, contributing

**Acceptance Criteria:**
- [ ] All directories created with placeholder README.md
- [ ] `ARCHITECTURE.md` split into `docs/architecture/overview.md`, `data-flow.md`, `mongodb-schema.md`

#### 3.3 Write Service-Specific CLAUDE.md
**Template:**
```markdown
# <Service Name> — Claude Code Context

## What this service does
<2-3 sentence description>

## Key files
| File | Purpose |
|------|---------|
| `src/main.py` | Entry point |
| `src/...` | ... |

## MongoDB
- **Database:** `interviews`
- **Watches:** `interviews.<collection>` — <trigger condition>
- **Writes:** `interviews.<collection>` — <schema>

## Environment variables
```
<VAR> — description
```

## Running locally (dev)
```bash
<commands>
```

## Running in Docker
```bash
docker compose up <service>
```

## Do not modify
- <file> — <reason>
- <file> — <reason>
```

**Acceptance Criteria:**
- [ ] All 4 services have project-specific CLAUDE.md
- [ ] Each CLAUDE.md includes: purpose, key files, MongoDB, env vars, run commands, do-not-modify list

---

### Phase 4: VNC Fix (High Effort)

**Goal:** Restore noVNC web interface for debugging

#### 4.1 Investigate Current State
**Files to review:**
- `Meeting-Bot/entrypoint.sh` — Xvfb + VNC + noVNC startup
- `Meeting-Bot/vnc_healthcheck.sh` — Health check logic
- `Meeting-Bot/novnc_index.html` — Landing page

**Commands to run:**
```bash
docker logs meeting-bot --tail 100
docker exec meeting-bot ps aux
docker exec meeting-bot netstat -tlnp
```

#### 4.2 Likely Fixes Needed
1. **Xvfb not starting** — Check `entrypoint.sh` Xvfb command
2. **x11vnc not binding** — Check display `:99` matches
3. **websockify not proxying** — Check noVNC WebSocket path
4. **noVNC landing page redirect** — Remove meta-refresh, manual Connect only

#### 4.3 Simplified Approach (Alternative)
If VNC is too complex, consider:
- Remove VNC entirely
- Use Playwright's built-in video recording for debugging
- Add `DEBUG_RECORD_MEETINGS=1` env var to enable/disable

**Acceptance Criteria:**
- [ ] Option A: noVNC works at `http://localhost:6080` — user clicks Connect, sees Chrome desktop
- [ ] Option B: VNC removed, Playwright recording enabled as alternative

---

### Phase 5: Testing & Validation (Final)

#### 5.1 Integration Test Suite
**New file:** `tests/e2e/interview-flow.test.py`

**Test flow:**
1. UI creates interview (POST `/start`)
2. Main-Agent inserts greeting
3. TTS processes greeting (mock ElevenLabs)
4. Simulate candidate transcript insert
5. Main-Agent generates response
6. TTS processes response
7. Interview completes (phase transitions work)

**Acceptance Criteria:**
- [ ] Full interview flow tested end-to-end
- [ ] All 5 phases tested (INTRO → EXPERIENCE → TECHNICAL → BEHAVIORAL → CLOSING)
- [ ] Interrupt feature tested (candidate speaks while TTS playing)

#### 5.2 Performance Benchmarks
**Metrics to track:**
- Main-Agent pipeline latency (target: <500ms p95)
- TTS synthesis latency (target: <2s for 100 chars)
- MongoDB change stream lag (target: <100ms)
- Meeting-Bot caption scrape delay (target: <1s from speech to transcript)

**Acceptance Criteria:**
- [ ] Benchmarks documented in `docs/operations/monitoring.md`
- [ ] All metrics meet targets

---

## Part 4: Decisions & Trade-offs

### ADR 001: MongoDB as Event Bus (Not NATS/RabbitMQ)

**Status:** Accepted  
**Date:** March 3, 2026

**Context:**
System needs event-driven communication between services. Options:
1. MongoDB Change Streams (current)
2. NATS (was in TTS code, never implemented)
3. RabbitMQ/Kafka (overkill for this scale)

**Decision:** MongoDB Change Streams

**Rationale:**
- Already running (no new infrastructure)
- ~50-100ms latency (acceptable for interview flow)
- Durable events (survive service restarts via resume tokens)
- Simple mental model (write to DB = fire event)

**Consequences:**
- MongoDB is single point of failure (mitigation: replica set in production)
- Change streams require replica set (added complexity)
- NATS code removed from TTS

---

### ADR 002: No Separate STT Service

**Status:** Accepted  
**Date:** March 3, 2026

**Context:**
Original architecture had STT as separate service. Google Meet provides native captions.

**Decision:** STT functionality embedded in Meeting-Bot

**Rationale:**
- Google Meet captions are free and accurate
- No need for separate Whisper/STT infrastructure
- Meeting-Bot already has Chrome DOM access
- Simpler architecture (one less service)

**Consequences:**
- Tied to Google Meet (can't use Zoom/Teams without new implementation)
- Caption quality depends on Google's speech recognition
- `STT/` directory intentionally not created (documented decision)

---

### ADR 003: Groq for LLM (Not Gemini/OpenAI/Anthropic)

**Status:** Proposed  
**Date:** March 3, 2026

**Context:**
Gemini free tier exhausted (15 RPM, 1000 requests/day). Need higher rate limits.

**Decision:** Switch to Groq (Llama 3.1 70B)

**Rationale:**
- 30 RPM free tier (2x Gemini)
- No daily request limit
- ~200ms latency (fastest API inference)
- Cheap paid tier (~$0.0001-0.001 per request)

**Consequences:**
- Need to implement `GroqProvider` class
- Llama 3.1 70B quality slightly below GPT-4/Claude (acceptable for interviews)
- Another API key to manage

---

## Part 5: Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Breaking changes during reorganization | Medium | High | Test after each phase, keep backups |
| VNC fix proves impossible | High | Medium | Fall back to Playwright recording |
| Groq quality insufficient | Low | Medium | Keep Gemini/OpenAI as fallback providers |
| Shared SDK becomes coupling point | Medium | Low | Keep SDK minimal (config, logging, models only) |
| Documentation becomes stale | High | Low | Assign ownership, review quarterly |

---

## Part 6: Execution Timeline

| Phase | Tasks | Estimated Time |
|-------|-------|----------------|
| **Phase 1: Immediate Fixes** | Groq provider, TTS cleanup, delete junk | 1-2 hours |
| **Phase 2: Reorganization** | `services/`, standardize structure, shared SDK | 4-6 hours |
| **Phase 3: Documentation** | `.specify/`, `docs/`, CLAUDE.md files | 2-3 hours |
| **Phase 4: VNC Fix** | Investigate, fix or remove | 2-4 hours |
| **Phase 5: Testing** | Integration tests, benchmarks | 3-4 hours |
| **Total** | | **12-19 hours** |

---

## Part 7: Next Steps

### When User Says "Execute":

1. **Start with Phase 1** (critical fixes first)
2. **Confirm after each phase** before proceeding
3. **Test thoroughly** before moving to next phase
4. **Create PHRs** for all significant changes
5. **Suggest ADRs** for architectural decisions

### First Commands (Phase 1):

```bash
# 1. Add Groq to requirements
echo "groq>=0.4.0" >> Main-Agent/requirements.txt

# 2. Remove NATS from TTS
# Edit TTS/requirements.txt — remove nats-py line

# 3. Delete junk files
del "*.md"
del "*.txt"  # Session logs
```

---

## Appendix A: File Inventory (Complete)

### Root Level (21 items)
```
.env
.env.example
.md                          ❌ DELETE (empty)
.git/
.qwen/
2026-03-02-165035-*.txt      ⚚ ARCHIVE
2026-03-03-004254-*.txt      ⚚ ARCHIVE
ARCHITECTURE-AND-CLEANUP-PLAN.md  ← This document
CLAUDE.md                    ❌ DELETE (generic template)
docker-compose.yml
image copy.png               ⚚ REVIEW (screenshot?)
image.png                    ⚚ REVIEW (screenshot?)
info.md                      ✅ KEEP
report.md                    ⚚ REVIEW
sessions/                    ⚚ REVIEW
Main-Agent/                  → Move to services/
Meeting-Bot/                 → Move to services/
STT/                         ❌ DOES NOT EXIST
TTS/                         → Move to services/
UI/                          → Move to services/
```

---

## Appendix B: Environment Variables (Complete)

### Root `.env` (All Services)

```env
# MongoDB (all services)
MONGODB_URI=mongodb://localhost:27017/?replicaSet=rs0
MONGODB_DB=interviews
TRANSCRIPTS_COLLECTION=transcripts

# Main-Agent
LLM_PROVIDER=gemini|openai|anthropic|groq
GEMINI_API_KEY=...
GEMINI_MODEL_CONVERSATION=gemini-2.0-flash
GEMINI_MODEL_EVALUATION=gemini-1.5-pro
GEMINI_MAX_OUTPUT_TOKENS=1000
GEMINI_TEMPERATURE=0.7
GEMINI_TOP_P=0.9
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4-turbo
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-4
GROQ_API_KEY=...
GROQ_MODEL=llama-3.1-70b-versatile

# Main-Agent (Safeguards)
SOFT_DURATION_LIMIT_MINUTES=90
HARD_DURATION_LIMIT_MINUTES=0
CONTEXT_WARNING_TOKENS=150000
CONTEXT_MAX_TOKENS=180000
SILENCE_TIMEOUT_SECONDS=180
MAX_CONCURRENT_INTERVIEWS=50
LLM_TIMEOUT_SECONDS=10

# Main-Agent (Cache)
CACHE_MAX_SIZE=1000
CACHE_TTL_SECONDS=7200

# Main-Agent (Logging)
LOG_FILE=  # Empty = stdout only

# TTS
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
ELEVENLABS_MODEL_ID=eleven_flash_v2_5
ELEVENLABS_OUTPUT_FORMAT=pcm_22050
AUDIO_DEVICE_ID=
PULSE_SERVER=unix:/run/pulse/native
VIRTUAL_MIC=virtual_mic
LOG_LEVEL=INFO

# Meeting-Bot
BOT_EMAIL=botprofileforproject@gmail.com

# UI
# (No UI-specific env vars)
```

---

**Document End**

This document is living — update as the architecture evolves.
