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
from redis_client import get_redis, RedisState

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
    """
    db = _get_db()
    result = await db["interviews"].update_one(
        {"interview_id": interview_id},
        {"$set": {"status": "abandoned", "ended_at": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Interview not found")
    return JSONResponse({"status": "stopped", "interview_id": interview_id})


@app.get("/status/{interview_id}")
async def interview_status(interview_id: str = Path(...)):
    """Return current status of an interview (Redis-first for instant updates)."""
    # Try Redis first (instant, real-time)
    state = await get_redis_state()
    if state:
        try:
            bot_status = await state.get_bot_status(interview_id)
            meeting_status = await state.get_meeting_status(interview_id)

            if bot_status or meeting_status:
                return JSONResponse({
                    "status": meeting_status.get("status", "unknown"),
                    "bot_status": bot_status.get("status", "pending"),
                    "source": "redis",  # For debugging
                    **bot_status,
                    **meeting_status
                })
        except:
            pass  # Fall back to MongoDB

    # Fallback to MongoDB
    db = _get_db()
    doc = await db["interviews"].find_one(
        {"interview_id": interview_id},
        {"status": 1, "bot_status": 1, "_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Interview not found")
    return JSONResponse({
        "status": doc.get("status"),
        "bot_status": doc.get("bot_status", "pending"),
        "source": "mongodb"  # For debugging
    })


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
        {"interview_id": 1, "status": 1, "started_at": 1, "phase": 1, "turn_count": 1, "_id": 0},
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


@app.get("/api/audio/{file_id}")
async def serve_audio(file_id: str = Path(...)):
    """Serve audio file from GridFS for playback in the UI."""
    import gridfs
    from bson import ObjectId
    db = _get_db()
    fs = gridfs.GridFS(db, collection='audio')
    try:
        data = fs.get(ObjectId(file_id)).read()
        from fastapi.responses import Response
        # Determine content type based on file extension in GridFS metadata
        file_doc = db['audio.files'].find_one({"_id": ObjectId(file_id)})
        filename = file_doc.get('filename', '') if file_doc else ''
        content_type = 'audio/mpeg' if filename.endswith('.mp3') else 'audio/wav'
        return Response(content=data, media_type=content_type)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Audio not found: {e}")
