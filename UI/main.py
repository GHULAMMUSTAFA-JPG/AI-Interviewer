"""
AI Interviewer — UI service (FastAPI + Jinja2).

Provides a form to configure and launch an interview session.
A single DB write fires the full event loop:
  - Main-Agent detects new interview → inserts greeting → TTS plays it
  - Meeting-Bot detects new interview → joins the meeting
"""

import io
import os
import subprocess
from uuid import uuid4
from datetime import datetime

import pdfplumber
import docx

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Path
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from redis_client import get_redis, RedisState, close_redis

load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = "interviews"

# Redis client (lazy initialization)
_redis_state = None

async def get_redis_state():
    """Get or create Redis state helper."""
    global _redis_state
    if _redis_state is None:
        try:
            redis = await get_redis()
            _redis_state = RedisState(redis)
        except:
            pass  # Redis not available, will use MongoDB
    return _redis_state

app = FastAPI(title="AI Interviewer")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Reuse a single Motor client for the lifetime of the process
_client: AsyncIOMotorClient | None = None


def _get_db():
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URI)
    return _client[DB_NAME]


def _extract_text(data: bytes, filename: str) -> str:
    """Extract plain text from a PDF or DOCX file."""
    name = filename.lower()
    if name.endswith(".pdf"):
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(
                page.extract_text() or "" for page in pdf.pages
            ).strip()
    if name.endswith(".docx"):
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs).strip()
    if name.endswith(".doc"):
        raise HTTPException(
            status_code=415,
            detail="Legacy .doc format is not supported. Please upload a .pdf or .docx file.",
        )
    raise HTTPException(
        status_code=415,
        detail=f"Unsupported file type: {filename}. Use .pdf or .docx.",
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/start")
async def start_interview(
    meeting_url: str = Form(...),
    job_description: str = Form(...),
    company_info: str = Form(...),
    candidate_cv: UploadFile = File(...),
):
    """
    Insert a new interview record into interviews.interviews.

    This single write triggers:
      1. Main-Agent greeting watcher → inserts greeting → TTS plays it
      2. Meeting-Bot watcher → joins the meeting URL
    """
    raw = await candidate_cv.read()
    cv_text = _extract_text(raw, candidate_cv.filename or "upload.pdf")

    interview_id = str(uuid4())
    db = _get_db()

    doc = {
        "interview_id": interview_id,
        "phase": "INTRO",
        "turn_count": 0,
        "status": "in_progress",
        "job_description": job_description,
        "company_info": company_info,
        "candidate_cv": cv_text,
        "meeting_url": meeting_url,
        "started_at": datetime.utcnow(),
    }

    await db["interviews"].insert_one(doc)

    # Return JSON for AJAX usage
    return JSONResponse({"status": "started", "interview_id": interview_id})


@app.post("/stop/{interview_id}")
async def stop_interview(interview_id: str = Path(...)):
    """
    Mark interview as abandoned. Meeting-Bot detects this and leaves the meeting.
    Also sets tts_interrupt=True so any ongoing TTS playback stops immediately.
    """
    db = _get_db()
    result = await db["interviews"].update_one(
        {"interview_id": interview_id},
        {"$set": {
            "status": "abandoned",
            "ended_at": datetime.utcnow(),
            "tts_interrupt": True,   # stop any active TTS playback immediately
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Interview not found")

    # Mirror to Redis immediately so UI polls reflect the change without MongoDB lag
    state = await get_redis_state()
    if state:
        try:
            await state.set_meeting_status(interview_id, "abandoned")
            await state.set_bot_status(interview_id, "abandoned")
        except Exception:
            pass

    return JSONResponse({"status": "stopped", "interview_id": interview_id})


@app.get("/status/{interview_id}")
async def interview_status(interview_id: str = Path(...)):
    """
    Return full real-time status of an interview.

    Redis is checked first for fast, live data. Falls back to MongoDB.
    Returns: status, bot_status, phase, turn_count, agent_status, tts_status.
    """
    db = _get_db()

    # Fetch MongoDB ground truth (phase/turn_count only come from here reliably)
    mongo_doc = await db["interviews"].find_one(
        {"interview_id": interview_id},
        {"status": 1, "bot_status": 1, "phase": 1, "turn_count": 1, "_id": 0},
    )
    if not mongo_doc:
        raise HTTPException(status_code=404, detail="Interview not found")

    result = {
        "status":       mongo_doc.get("status", "unknown"),
        "bot_status":   mongo_doc.get("bot_status", "pending"),
        "phase":        mongo_doc.get("phase", "INTRO"),
        "turn_count":   mongo_doc.get("turn_count", 0),
        "agent_status": "idle",
        "tts_status":   "idle",
        "source":       "mongodb",
    }

    # Overlay Redis for instant component-level updates
    state = await get_redis_state()
    if state:
        try:
            redis = await get_redis()

            bot_r = await state.get_bot_status(interview_id)
            if bot_r and bot_r.get("status"):
                result["bot_status"] = bot_r["status"]
                result["source"] = "redis"

            meeting_r = await state.get_meeting_status(interview_id)
            if meeting_r and meeting_r.get("status"):
                result["status"] = meeting_r["status"]
                result["source"] = "redis"

            agent_r = await redis.hgetall(f"agent:{interview_id}:status")
            if agent_r and agent_r.get("status"):
                result["agent_status"] = agent_r["status"]

            tts_r = await redis.hgetall(f"tts:{interview_id}:status")
            if tts_r and tts_r.get("status"):
                result["tts_status"] = tts_r["status"]

            # Interview state written by pipeline.py (phase/turn override)
            iv_r = await redis.hgetall(f"interview:{interview_id}:state")
            if iv_r:
                if iv_r.get("phase"):
                    result["phase"] = iv_r["phase"]
                if iv_r.get("turn_count"):
                    result["turn_count"] = int(iv_r["turn_count"])

        except Exception as redis_err:
            print(f"⚠️  Redis status error for {interview_id}: {redis_err}")

    return JSONResponse(result)


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """Show log viewer page."""
    services = [
        {"name": "cleanup-service", "label": "Cleanup Service (Watchdog)"},
        {"name": "meeting-bot", "label": "Meeting-Bot"},
        {"name": "interview-agent", "label": "Main-Agent (LLM)"},
        {"name": "tts", "label": "TTS (Audio)"},
        {"name": "mongodb", "label": "MongoDB"},
        {"name": "ui", "label": "UI (Web)"},
    ]
    return templates.TemplateResponse(
        "logs.html", {"request": request, "services": services}
    )


@app.get("/api/logs")
async def get_logs(service: str, lines: int = 100):
    """
    Fetch logs from Docker for a specific service.

    Args:
        service: Service name (cleanup-service, meeting-bot, etc.)
        lines: Number of log lines to fetch (default: 100)

    Returns:
        JSON with log lines as array of strings
    """
    # Whitelist of allowed services (prevent command injection)
    ALLOWED_SERVICES = {
        "cleanup-service",
        "meeting-bot",
        "interview-agent",
        "tts",
        "mongodb",
        "ui",
    }

    if service not in ALLOWED_SERVICES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid service. Must be one of: {', '.join(ALLOWED_SERVICES)}"
        )

    try:
        # Run docker compose logs command
        result = subprocess.run(
            ["docker", "compose", "logs", "--tail", str(lines), service],
            capture_output=True,
            text=True,
            timeout=10,  # Timeout after 10 seconds
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )

        if result.returncode != 0:
            # Return error message as log line
            return JSONResponse({
                "service": service,
                "lines": [f"Error fetching logs: {result.stderr.strip()}"],
                "error": True
            })

        # Parse log output (format: "service-name  |  log message")
        log_lines = []
        for line in result.stdout.splitlines():
            # Remove service prefix if present
            if "  |  " in line:
                log_lines.append(line.split("  |  ", 1)[1])
            elif line.strip():
                log_lines.append(line)

        return JSONResponse({
            "service": service,
            "lines": log_lines,
            "error": False
        })

    except subprocess.TimeoutExpired:
        return JSONResponse({
            "service": service,
            "lines": ["Error: Log fetch timed out after 10 seconds"],
            "error": True
        })
    except Exception as e:
        return JSONResponse({
            "service": service,
            "lines": [f"Error: {str(e)}"],
            "error": True
        })


@app.get("/conversations", response_class=HTMLResponse)
async def list_conversations(request: Request):
    """List all interviews with links to their conversation view."""
    db = _get_db()
    interviews = await db["interviews"].find(
        {},
        {"interview_id": 1, "status": 1, "bot_status": 1, "started_at": 1, "phase": 1, "turn_count": 1, "_id": 0},
    ).sort("started_at", -1).to_list(length=100)
    return templates.TemplateResponse(
        "conversations.html", {"request": request, "interviews": interviews}
    )


@app.get("/conversation/{interview_id}", response_class=HTMLResponse)
async def view_conversation(request: Request, interview_id: str = Path(...)):
    """Show the full transcript for one interview. Auto-refreshes every 5s while in_progress."""
    db = _get_db()
    interview = await db["interviews"].find_one(
        {"interview_id": interview_id},
        {"interview_id": 1, "status": 1, "started_at": 1, "phase": 1, "turn_count": 1, "_id": 0},
    )
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")

    messages = await db["transcripts"].find(
        {"interview_id": interview_id},
        {"speaker": 1, "text": 1, "timestamp": 1, "audio_url": 1, "_id": 1},
    ).sort("timestamp", 1).to_list(length=None)

    return templates.TemplateResponse(
        "conversation.html",
        {"request": request, "interview": interview, "messages": messages},
    )


@app.get("/evaluations", response_class=HTMLResponse)
async def list_evaluations(request: Request):
    """List all interviews that have a completed evaluation."""
    db = _get_db()
    cursor = db["interviews"].find(
        {"evaluation": {"$exists": True}},
        {
            "interview_id": 1,
            "status": 1,
            "phase": 1,
            "turn_count": 1,
            "started_at": 1,
            "ended_at": 1,
            "candidate_cv": 1,
            "evaluation.recommendation": 1,
            "evaluation.score": 1,
            "evaluation.generated_at": 1,
        },
    ).sort("started_at", -1).limit(100)
    evaluations = await cursor.to_list(length=100)
    for ev in evaluations:
        ev["_id"] = str(ev["_id"])
        if ev.get("started_at") and ev.get("ended_at"):
            delta = ev["ended_at"] - ev["started_at"]
            ev["duration_min"] = round(delta.total_seconds() / 60, 1)
        else:
            ev["duration_min"] = None
    return templates.TemplateResponse(
        "evaluations.html", {"request": request, "evaluations": evaluations}
    )


@app.get("/evaluation/{interview_id}", response_class=HTMLResponse)
async def view_evaluation(request: Request, interview_id: str = Path(...)):
    """Show full evaluation card + complete conversation for one interview."""
    db = _get_db()
    interview = await db["interviews"].find_one({"interview_id": interview_id})
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")
    interview["_id"] = str(interview["_id"])

    messages = await db["transcripts"].find(
        {"interview_id": interview_id},
        {"speaker": 1, "text": 1, "timestamp": 1, "audio_url": 1, "_id": 1},
    ).sort("timestamp", 1).to_list(length=None)

    for msg in messages:
        msg["_id"] = str(msg["_id"])
        raw_url = msg.get("audio_url")
        msg["has_audio"] = bool(
            raw_url and raw_url not in ("played", "tts_skipped")
        )

    duration_min = None
    if interview.get("started_at") and interview.get("ended_at"):
        delta = interview["ended_at"] - interview["started_at"]
        duration_min = round(delta.total_seconds() / 60, 1)

    return templates.TemplateResponse(
        "evaluation.html",
        {
            "request": request,
            "interview": interview,
            "messages": messages,
            "evaluation": interview.get("evaluation", {}),
            "duration_min": duration_min,
        },
    )


@app.get("/api/audio/{file_id}")
async def serve_audio(file_id: str = Path(...)):
    """Serve audio file from GridFS for playback in the UI."""
    import io
    import wave
    from bson import ObjectId
    from fastapi.responses import Response
    from motor.motor_asyncio import AsyncIOMotorGridFSBucket

    db = _get_db()
    try:
        oid = ObjectId(file_id)

        # Use Motor's async GridFS — sync gridfs.GridFS cannot accept a Motor database
        bucket = AsyncIOMotorGridFSBucket(db, bucket_name="audio")

        # Fetch file metadata to determine content type
        file_doc = await db["audio.files"].find_one({"_id": oid})
        if not file_doc:
            raise HTTPException(status_code=404, detail="Audio not found")

        # Download audio bytes asynchronously
        grid_out = await bucket.open_download_stream(oid)
        data = await grid_out.read()

        # Determine content type — PCM needs a WAV wrapper for browser playback
        filename = file_doc.get("filename", "")
        if filename.endswith(".mp3"):
            content_type = "audio/mpeg"
        elif filename.endswith(".pcm"):
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)   # 16-bit
                wf.setframerate(22050)
                wf.writeframes(data)
            data = wav_buffer.getvalue()
            content_type = "audio/wav"
        else:
            content_type = "audio/wav"

        return Response(content=data, media_type=content_type)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Audio not found: {e}")
