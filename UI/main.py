"""
AI Interviewer — UI service (FastAPI + Jinja2).

Provides a form to configure and launch an interview session.
A single DB write fires the full event loop:
  - Main-Agent detects new interview → inserts greeting → TTS plays it
  - Meeting-Bot detects new interview → joins the meeting
"""

import io
import os
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

load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = "interviews"

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
