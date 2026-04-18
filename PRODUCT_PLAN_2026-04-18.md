# AI Interviewer — Product Build Plan
**Date:** 2026-04-18
**Status:** Active — ready to implement
**Purpose:** Turn the working AI interviewer into a real product people can buy and use

---

# PLAIN LANGUAGE OVERVIEW

## What you have right now

A fully working AI interviewer that:
- Joins a Google Meet call as a bot
- Listens to a candidate speaking (using Web Speech API injected into Chrome — NOT caption scraping)
- Generates smart interview questions and follow-ups using Gemini AI
- Speaks back using Edge TTS through the meeting audio
- Conducts a full structured interview across 5 phases (Intro → Experience → Technical → Behavioral → Closing)
- Produces a hire/no-hire recommendation at the end
- Has crash recovery, echo guard, interruption handling, circuit breaker — all built

**You already have more than most commercial AI interview products.**

The website at `localhost:8080` already has:
- Interview setup form
- Live status page (shows what phase the bot is in, real-time)
- Conversations list (all past interviews)
- Transcript viewer (read the full conversation)
- Evaluations list (hire/no-hire results)
- Evaluation detail page (shows recommendation + strengths + concerns)
- Log viewer (debug any service)

## What is missing to make it a real product

**4 things, in the order you should build them:**

---

### Thing 1: Make the interview report look professional
**What:** The evaluation page already exists but looks basic. Recruiters need a report they would actually forward to a hiring manager — with competency scores, evidence from the transcript, phase-by-phase breakdown, and good visual design.
**Why first:** This is the thing people will pay for. Everything else supports this.
**Time:** 2-3 days

### Thing 2: Run more than one interview at a time
**What:** Right now only one interview can happen at a time because Chrome and the audio system are shared. You need to fix this so 5, 10, or 50 interviews can run simultaneously.
**Why second:** Without this, you cannot serve more than one customer at a time.
**Time:** 3-5 days

### Thing 3: Add login and separate customers
**What:** Anyone can currently open the website. You need email + password login and each company should only see their own interviews.
**Why third:** Needed before you give access to a second company.
**Time:** 2-3 days

### Thing 4: Build a proper recruiter dashboard
**What:** A clean page showing all interviews by candidate name, job role, date, and result. Plus a way to invite candidates via email with a meeting link.
**Why fourth:** Makes the product feel finished and professional.
**Time:** 2-3 days

---

### Small fixes to do throughout (not blocking, but needed)
- Add passwords to MongoDB and Redis (wide open right now) — half a day
- Add HTTPS to the website — half a day
- Add `interview_id` to logs in TTS and Meeting-Bot services — 1 day
- Add a database index on transcripts so searches stay fast as data grows — 10 minutes
- Delete the 15 dead git branches — 30 minutes

---

### Things NOT to build yet

- **Do not use Kubernetes — postponed indefinitely.** Docker Compose will handle this system for a very long time. Kubernetes adds significant complexity (networking, secrets, pod specs, PulseAudio sidecars) that is hard to debug when something breaks.
- **ElevenLabs is future only.** Edge TTS is the active synthesizer. ElevenLabs file (`elevenlabs_synthesizer.py`) exists in TTS but is inactive. It will be activated in a later phase for better voice quality, not now.
- Do not replace Web Speech API — it works well and has been tuned heavily
- Do not connect to Greenhouse/Lever/Workday ATS — email export is fine for now
- Do not add Redis pub/sub pipeline triggers (MongoDB change streams are the rule)
- Do not add billing/Stripe yet — get first paying customer manually

---

## Total estimated time
**12-14 days of focused work**, producing a product that multiple customers can use, with login, parallel interviews, a professional report, and monitoring — all on Docker Compose.

---

# TECHNICAL DETAILS

## Current State — What's Actually Built

### Services (7 containers, all healthy as of 2026-04-18)
| Service | Tech | Role |
|---|---|---|
| UI | FastAPI + Jinja2, port 8080 | Web form, status pages, evaluation pages |
| Main-Agent | Python, Gemini 2.0 flash-lite | LLM brain, phase management, evaluation |
| TTS | Python, Edge TTS | Synthesizes speech → PulseAudio → Chrome |
| Meeting-Bot | Python, Playwright, Chrome | Joins Meet, JS-injected STT, audio routing |
| Redis | redis:7-alpine | State, echo guard, interrupt signals, heartbeat |
| MongoDB | mongo:7.0, replica set rs0 | Event bus (change streams) + persistent storage |
| Cleanup-Service | Python | Detects stale/crashed interviews, marks abandoned |

### UI routes already built (no auth on any of them)
- `GET /` — interview setup form
- `POST /start` — creates interview (fires full pipeline)
- `POST /stop/{id}` — marks interview abandoned
- `GET /status/{id}/stream` — SSE live status feed
- `GET /conversations` — list all interviews
- `GET /conversation/{id}` — full transcript view
- `GET /evaluations` — list evaluations
- `GET /evaluation/{id}` — evaluation card + transcript
- `GET /logs` — log viewer
- `GET /api/logs` — fetch docker service logs

### Evaluator output (current — Main-Agent/src/agent/evaluator.py)
```python
{
    "recommendation": "HIRE" | "MAYBE" | "NO_HIRE",
    "score": 1-10,
    "strengths": ["...", "...", "..."],      # plain text list
    "concerns": ["...", "...", "..."],       # plain text list
    "reasoning": "2-3 sentence explanation",
    "generated_at": datetime
}
```
**Missing:** competency scores with evidence quotes, phase summaries, follow-up questions, confidence level.

### Interview phases (Main-Agent/src/agent/phases.py)
INTRO (2-3 turns) → EXPERIENCE (3-8) → TECHNICAL (4-10) → BEHAVIORAL (3-8) → CLOSING (3-6)

### Key parallel interview blockers
1. **Chrome profile lock** — `Meeting-Bot/join_meeting.py line 98`: `chrome_profile_dir = "/app/chrome_profile"`. Fix: `f"/app/chrome_profile_{interview_id}"`.
2. **PulseAudio global sinks** — 4 hardcoded sinks in `Meeting-Bot/entrypoint.sh`: VirtualSink, BotMic, virtual_mic, virtual_mic_source. Fix: container-per-interview (each container gets its own isolated audio).

---

## Build Plan: Thing 1 — Professional Scorecard

**File: `Main-Agent/src/agent/evaluator.py`**
- Use Gemini structured output (`response_schema`) instead of text parsing
- Add competencies with scores (1-5) + evidence quotes from transcript
- Add phase-by-phase summaries, follow-up questions, confidence, red_flags

**Target evaluation output:**
```python
{
    "recommendation": "hire" | "maybe" | "no_hire",
    "confidence": 0.0-1.0,
    "headline": "One sentence summary",
    "score": 1-10,
    "competencies": [
        {"name": "Technical Depth", "score": 1-5, "evidence": "quote"},
        {"name": "Communication", "score": 1-5, "evidence": "quote"},
        {"name": "Problem Solving", "score": 1-5, "evidence": "quote"},
        {"name": "Cultural Fit", "score": 1-5, "evidence": "quote"},
        {"name": "Experience Relevance", "score": 1-5, "evidence": "quote"}
    ],
    "strengths": ["...", "..."],
    "concerns": ["...", "..."],
    "red_flags": [],
    "reasoning": "2-3 sentences",
    "suggested_followups": ["..."],
    "phase_summaries": {"INTRO": "...", "EXPERIENCE": "...", "TECHNICAL": "...", "BEHAVIORAL": "...", "CLOSING": "..."},
    "generated_at": datetime
}
```

**File: `UI/templates/evaluation.html`**
- Competency bar chart with evidence quotes
- Phase-by-phase summary accordion
- Follow-up questions section
- PDF/print button
- Professional HR document layout

---

## Build Plan: Thing 2 — Parallel Interviews (Container-per-interview)

**Architecture:** One Docker container per active interview. Each container gets its own Chrome + PulseAudio. No global audio state conflicts.

**New file: `Meeting-Bot/orchestrator.py`**
- Watches `interviews.interviews` change stream for `status=in_progress` inserts
- Calls Docker API to spin up a fresh `meeting-bot` container with `INTERVIEW_ID` env var
- Monitors heartbeat in Redis; marks interview abandoned if container dies

**Change: `Meeting-Bot/main.py`**
- Single-interview mode: read `INTERVIEW_ID` from env var → run one interview → exit

**Change: `Meeting-Bot/join_meeting.py` line 98**
```python
chrome_profile_dir = f"/app/chrome_profile_{interview_id}"
```

**Change: `docker-compose.yml`**
- Meeting-bot becomes a container template, not a persistent service
- Orchestrator becomes the long-running watcher service

---

## Build Plan: Thing 3 — Login and Multi-Tenancy

**New MongoDB collections:**
- `interviews.organizations` — `{_id, name, created_at}`
- `interviews.users` — `{_id, email, hashed_password, org_id, role: "admin"|"recruiter"}`

**Changes to `interviews.interviews`:** add `org_id` field; all queries scoped to org

**Changes to `UI/main.py`:**
- Session-based auth (itsdangerous session cookies)
- `POST /auth/login`, `POST /auth/logout`, `GET /auth/register`
- Middleware: check session on all non-auth routes

**New templates:** `login.html`, `register.html`

---

## Build Plan: Thing 4 — Recruiter Dashboard

**New route: `GET /dashboard`**
- Lists interviews filtered by org_id
- Search by candidate name / job role / date
- Stats: total this month, avg score, hire rate

**New route: `POST /invite`**
- Send candidate an email with a meeting link
- Creates interview document in advance

**New template: `dashboard.html`**
- Table: Candidate | Role | Date | Duration | Score | Recommendation | Status
- Stats cards at top

---

## Small Fixes — Exact Locations

| Fix | File | Change |
|---|---|---|
| MongoDB index | `Main-Agent/src/main.py` startup | `create_index([("interview_id",1),("speaker",1),("created_at",-1)])` |
| MongoDB auth | `docker-compose.yml` | `MONGO_INITDB_ROOT_USERNAME/PASSWORD` + update all URIs |
| Redis auth | `docker/redis/redis.conf` | `requirepass <password>` + update all redis:// URIs |
| interview_id in TTS logs | `TTS/src/tts/main.py` | Pass interview_id into every log call |
| interview_id in Meeting-Bot logs | `Meeting-Bot/logger.py` | Add interview_id parameter to push_log() |
| Delete zombie branches | git | All 15 feature/* branches except feature/product-quality |

---

## Build Sequence

| Day | Work |
|-----|------|
| 1-2 | Scorecard: rewrite evaluator.py + redesign evaluation.html |
| 3 | Small fixes: DB index, branch cleanup, logs, MongoDB/Redis auth |
| 4-6 | Parallel interviews: orchestrator + single-interview mode + chrome profile fix |
| 7-8 | Login + multi-tenancy: auth routes, org scoping, login/register templates |
| 9-10 | Dashboard + candidate invite by email |
| 11-12 | Polish + nginx + HTTPS |
| 13-14 | Monitoring: Prometheus + Grafana + Alertmanager on Docker Compose (no Kubernetes) |

---

## Monitoring Plan (Day 13-14) — Docker Compose Only

Prometheus + Grafana + Alertmanager as 3 additional Docker containers alongside the existing services. No Kubernetes.

**What you get:**
- Dashboard showing active interviews, LLM response time, TTS duration, circuit breaker state
- Slack/email alert when any interview goes into degraded state or bot crashes
- 30-day log retention

**Key metrics:**
- `active_interviews_total` (Meeting-Bot)
- `llm_call_duration_seconds` (Main-Agent)
- `tts_synthesis_duration_seconds` (TTS)
- `circuit_breaker_open_total` (Main-Agent)
- `interview_completed_total` (Main-Agent)

---

## Branch Strategy
- All work on new branch: `feature/product-v2`
- Created from `feature/product-quality` (current, clean)
- Never merge to master until full product is stable

## Rules
- MongoDB change streams are the mandatory event bus — no Redis pub/sub bypass
- TTS: Edge TTS only — ElevenLabs stays inactive until future phase
- LLM: `gemini-2.0-flash-lite` — do not change
- No Kubernetes — Docker Compose only, indefinitely
