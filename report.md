# AI Interviewer — System Report

**Generated:** 2026-02-26
**Status:** All containers running

---

## 1. MongoDB — Change Stream Configuration

### Is it configured for change streams?

**Yes — fully configured.**

Change streams require a MongoDB **replica set**. This is set up in two steps inside `docker-compose.yml`:

**Step 1 — MongoDB starts with replica set flag:**
```yaml
mongodb:
  image: mongo:7.0
  command: ["--replSet", "rs0", "--bind_ip_all"]
```

**Step 2 — `mongo-init` container initialises the set on first boot:**
```js
try { rs.status(); print('rs0 already initialised'); }
catch(e) {
  rs.initiate({ _id: 'rs0', members: [{ _id: 0, host: 'mongodb:27017' }] });
}
```

### Verified live state

```
Set name : rs0
Members  : 1
Primary  : mongodb:27017  [PRIMARY]
Change streams: SUPPORTED
```

All four services that use change streams connect with:
```
mongodb://mongodb:27017/?replicaSet=rs0
```

---

## 2. Connecting to MongoDB from your local machine (Compass)

The replica set advertises itself as `mongodb:27017` (Docker internal hostname).
Compass follows that redirect and fails because your machine doesn't know that hostname.

**Fix — use `directConnection=true`:**

```
mongodb://localhost:27017/?directConnection=true
```

Paste that string into Compass → "New Connection" → paste → Connect.
`directConnection=true` tells the driver to talk to the node directly, skipping replica-set routing.

---

## 3. Database Design

### Database: `interviews`

All services read/write to the same database. Collections are created automatically on first insert.

---

### Collection: `interviews`

Created by the **UI** when the user submits the form. Triggers both Meeting-Bot and Main-Agent greeting watcher via change stream.

```json
{
  "_id": ObjectId,
  "interview_id": "550e8400-e29b-41d4-a716-446655440000",  // UUID string
  "phase": "INTRO",          // INTRO | EXPERIENCE | TECHNICAL | BEHAVIORAL | CLOSING
  "turn_count": 0,           // incremented by Main-Agent after each agent reply
  "status": "in_progress",  // in_progress | completed | abandoned
  "job_description": "...",
  "company_info": "...",
  "candidate_cv": "...",
  "meeting_url": "https://meet.google.com/xxx-yyy-zzz",
  "started_at": ISODate
}
```

**Change stream watchers:**
- `Meeting-Bot` → filter: `operationType=insert AND status=in_progress`
- `Main-Agent (greeting)` → filter: `operationType=insert`

---

### Collection: `transcripts`

The shared event bus. Every spoken line — candidate or agent — lives here.
Inserting here is how services signal each other.

```json
{
  "_id": ObjectId,
  "interview_id": "550e8400-e29b-41d4-a716-446655440000",
  "speaker": "candidate",   // "candidate" | "agent"
  "text": "Hello, my name is John and I have 5 years of experience...",
  "audio_url": null,        // null = TTS hasn't played it yet | string = played
  "timestamp": ISODate
}
```

**Change stream watchers:**
- `Main-Agent (candidate watcher)` → filter: `insert AND speaker=candidate`
- `TTS` → filter: `insert AND speaker=agent AND audio_url=null`

**Why `audio_url=null`?**
TTS sets `audio_url` to a non-null value after it plays the audio.
This prevents replaying the same line if the service restarts.

---

### Collection: `evaluations`

Written by **Main-Agent** at the end of the interview (when phase reaches CLOSING and turn limit is hit).

```json
{
  "_id": ObjectId,
  "interview_id": "...",
  "recommendation": "HIRE",        // HIRE | NO_HIRE | MAYBE
  "score": 82,                     // 0–100
  "strengths": ["Technical depth", "Clear communication"],
  "concerns": ["Limited leadership experience"],
  "reasoning": "Candidate demonstrated strong Python fundamentals...",
  "candidate_cv": "...",
  "job_description": "...",
  "conversation_summary": "...",
  "created_at": ISODate
}
```

---

### Collection: `agent_state`

Used by **Main-Agent** only. Persists the MongoDB change stream resume token so the agent can restart without replaying old events or missing new ones.

```json
{
  "_id": "resume_token",
  "token": { "_data": "..." }   // opaque MongoDB resume token
}
```

---

## 4. Full Event Flow

```
Browser → http://localhost:8080  (UI form)
  │
  └── POST /start
        └── INSERT interviews.interviews
               │
               ├─── Main-Agent (greeting watcher fires)
               │      └── INSERT transcripts {speaker:"agent", text:"Hello! Welcome..."}
               │               │
               │               └── TTS (speaker=agent, audio_url=null fires)
               │                     └── ElevenLabs API → PCM audio → PulseAudio
               │                           └── Chrome plays greeting in meeting
               │
               └─── Meeting-Bot (new interview fires)
                      └── Playwright Chrome joins meeting_url
                            └── caption scraper polls DOM every 1s
                                  │
                                  [candidate speaks]
                                  │
                                  └── utterance buffer (debounce 4s)
                                        └── INSERT transcripts {speaker:"candidate"}
                                               │
                                               └── Main-Agent (candidate watcher fires)
                                                     └── LLM (Gemini 2.5 flash)
                                                           └── INSERT transcripts {speaker:"agent"}
                                                                  │
                                                                  └── TTS plays response
                                                                        └── loop ↑
```

---

## 5. Service Status

| Container | Image | Status | Port |
|---|---|---|---|
| `mongodb` | mongo:7.0 | Healthy — PRIMARY | 27017 |
| `mongo-init` | mongo:7.0 | Completed (one-shot) | — |
| `interview-agent` | aiinterviwer-interview-agent | Running | — |
| `tts` | aiinterviwer-tts | Running | — |
| `meeting-bot` | aiinterviwer-meeting-bot | Running | — |
| `ui` | aiinterviwer-ui | Running | 8080 |

**UI:** http://localhost:8080
**MongoDB (Compass):** `mongodb://localhost:27017/?directConnection=true`

---

## 6. Image Sizes

| Image | Size |
|---|---|
| `aiinterviwer-meeting-bot` | 3.93 GB (Ubuntu + Chrome + Playwright) |
| `aiinterviwer-tts` | 552 MB |
| `aiinterviwer-interview-agent` | 358 MB |
| `aiinterviwer-ui` | 263 MB |
| `mongo:7.0` | 1.18 GB |

---

## 7. How to Operate

### Start everything
```bash
cd "D:/PF-24.2/AI Interviwer"
docker compose up -d
```

### Start with fresh build
```bash
docker compose up --build -d
```

### Watch all logs
```bash
docker compose logs -f
```

### Watch one service
```bash
docker logs interview-agent -f
docker logs tts -f
docker logs meeting-bot -f
```

### Stop everything (keeps data)
```bash
docker compose down
```

### Stop everything and wipe DB
```bash
docker compose down -v
```

### Rebuild one service after code change
```bash
docker compose up --build tts -d
docker compose up --build interview-agent -d
docker compose up --build meeting-bot -d
docker compose up --build ui -d
```

---

## 8. Environment Variables (root `.env`)

| Variable | Used by | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | interview-agent | LLM calls |
| `ELEVENLABS_API_KEY` | tts | Speech synthesis |
| `ELEVENLABS_VOICE_ID` | tts | Which voice to use |
| `BOT_EMAIL` | meeting-bot | Bot display name in meeting |
| `MONGODB_URI` | all | Overridden per-service in compose |
| `MONGODB_DB` | all | `interviews` |

---

## 9. Known Limitations

| Issue | Impact | Fix |
|---|---|---|
| PulseAudio not available in Docker on Windows | TTS can't route audio to Chrome virtual mic | Run TTS natively on host, or use Linux host with socket mount |
| Meeting-Bot Chrome requires real Google Meet admission | Bot waits up to 10 min to be admitted | Host must admit bot from waiting room |
| MongoDB replica set advertised as `mongodb:27017` | Compass can't connect with default string | Use `?directConnection=true` (documented above) |
| ElevenLabs free tier rate limits | TTS may slow down on long interviews | Upgrade ElevenLabs plan |
