# Meeting-Bot — Claude Code Context

## What this service does
Persistent change-stream service. Watches `interviews.interviews` for new inserts,
spawns a headless Chrome browser per interview, joins the Google Meet, scrapes live
captions from the DOM, and writes candidate speech to `interviews.transcripts`.

## Key files
| File | Purpose |
|---|---|
| `main.py` | Entry point — change stream loop, spawns `run_bot()` per interview |
| `bot.py` | Playwright browser logic — joins meeting, filters bot speech, calls `on_caption` |
| `caption_scraper.py` | DOM caption extraction — stabilization buffer, dedup, `scrape_meeting_captions()` |
| `mongo_handler.py` | DB layer — writes ONLY to `interviews.transcripts` with `speaker="candidate"` |
| `logger.py` | Thin async logger |
| `llm_handler.py` | Post-meeting Gemini summary (optional, not part of the live loop) |
| `entrypoint.sh` | Docker entrypoint — starts Xvfb (:99) + PulseAudio before Python |

## MongoDB
- **Database:** `interviews`
- **Watches:** `interviews.interviews` — triggers on insert, status="in_progress"
- **Writes:** `interviews.transcripts` — `{interview_id, speaker:"candidate", text, audio_url:null, timestamp}`
- **Never touches:** `Ai.*`, `speech-to-text-component.*` (those are STT legacy collections)

## Speaker filter (critical)
In `bot.py → on_caption()`: any speaker whose name contains `(AI)` (case-insensitive)
is skipped. The bot joins as `<email_prefix> (AI)`, so its own TTS audio (which Chrome
picks up and captions) is filtered out. Only the human candidate's speech is saved.

## Utterance buffering (turn detection)
The caption scraper fires `on_caption` once per stabilized DOM chunk (~1.5 s).
One candidate turn ("Hello. I have 5 years experience. Let me explain...") produces
multiple chunks — each would trigger the Main-Agent mid-thought without buffering.

**Fix in `bot.py → on_caption()`:**
- Each chunk is appended to a per-speaker `parts` list.
- A 4-second silence timer (`UTTERANCE_SILENCE_SEC`) is started/reset on every chunk.
- When the timer fires (real silence = candidate stopped talking), all parts are joined
  and saved as **one** `interviews.transcripts` insert.
- Main-Agent is triggered exactly once per complete candidate turn.
- On meeting end, any still-buffered utterances are flushed immediately.

To adjust the silence threshold change `UTTERANCE_SILENCE_SEC` in `bot.py`.

## Change stream trigger condition
```python
# main.py
pipeline = [{"$match": {"operationType": "insert"}}]
# + filter: interview["status"] == "in_progress"
```

## Environment variables
```
MONGODB_URI   — connection string (auto-overridden in Docker to mongodb://mongodb:27017/?replicaSet=rs0)
BOT_EMAIL     — email prefix becomes the bot's display name in the meeting
GEMINI_API_KEY — only used for post-meeting summary generation
```

## Running locally (dev)
```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # fill in MONGODB_URI and BOT_EMAIL
python main.py
```

## Running in Docker
```bash
# From repo root:
docker compose up --build meeting-bot
```

## Do not modify
- `caption_scraper.py` — complex stabilization logic, treat as stable
- The `(AI)` speaker filter in `bot.py` — breaks the loop if removed
- The `interviews.transcripts` schema — Main-Agent and TTS depend on it
