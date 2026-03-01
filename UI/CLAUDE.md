# UI — Claude Code Context

## What this service does
FastAPI + Jinja2 web form. The user fills in meeting details and clicks Submit.
One `INSERT` into `interviews.interviews` fires the entire pipeline — no other
API calls, no polling, no WebSockets needed.

## Key files
| File | Purpose |
|---|---|
| `main.py` | FastAPI app — `GET /` (form) and `POST /start` (creates interview) |
| `templates/index.html` | Jinja2 form — Meeting URL, Job Description, Company Info, Candidate CV |
| `static/style.css` | Minimal styles |
| `requirements.txt` | fastapi, uvicorn, jinja2, motor, python-dotenv, python-multipart |

## The single DB write that starts everything
`POST /start` inserts into `interviews.interviews`:
```json
{
  "interview_id": "<uuid4>",
  "phase": "INTRO",
  "turn_count": 0,
  "status": "in_progress",
  "job_description": "...",
  "company_info": "...",
  "candidate_cv": "...",
  "meeting_url": "https://meet.google.com/xxx-yyy-zzz",
  "started_at": "<ISODate>"
}
```
This fires two change streams simultaneously:
1. **interview-agent** (greeting watcher) → inserts opening greeting → TTS plays it
2. **meeting-bot** → joins the meeting URL

## MongoDB
- **Database:** `interviews`
- **Writes:** `interviews.interviews` only
- **Reads:** nothing (fire-and-forget)

## Environment variables
```
MONGODB_URI — connection string (auto-overridden in Docker)
```

## Running locally (dev)
```bash
pip install -r requirements.txt
cp .env.example .env  # fill in MONGODB_URI
uvicorn main:app --reload --port 8080
```
Open http://localhost:8080

## Running in Docker
```bash
# From repo root:
docker compose up --build ui
```
Open http://localhost:8080

## Adding new form fields
1. Add the HTML `<input>` or `<textarea>` in `templates/index.html`
2. Add the corresponding `Form(...)` parameter in `main.py → start_interview()`
3. Include the field in the `doc` dict inserted into MongoDB
4. Make sure Main-Agent reads the new field from `interviews.interviews`
   (it uses `candidate_cv`, `job_description`, `company_info` to build the prompt)

## Do not change
- The `status: "in_progress"` field — Meeting-Bot and Main-Agent filter on this
- The `interview_id` field — all services use it as the shared key
- The collection name `interviews` — hardcoded in compose MONGODB_URI override
