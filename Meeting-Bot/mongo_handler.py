"""
MongoDB handler for Meeting-Bot.
Writes candidate captions to interviews.transcripts only.
This collection is the shared event bus between all services.
"""

import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket
from pymongo.errors import ServerSelectionTimeoutError
from bson import ObjectId
from datetime import datetime
from dotenv import load_dotenv
from logger import push_log

load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = "interviews"

client = None
db = None

# 9.1: asyncio.Queue fallback — failed inserts queue here and retry in background.
# Prevents candidate speech from being lost during MongoDB blips (replica elections).
_fallback_queue: asyncio.Queue = asyncio.Queue()
_retry_task: asyncio.Task | None = None


async def start_fallback_retry_task() -> None:
    """Start the background retry task. Call once at service startup."""
    global _retry_task
    if _retry_task is None or _retry_task.done():
        _retry_task = asyncio.create_task(_retry_failed_inserts())


async def _retry_failed_inserts() -> None:
    """Background task: retry queued inserts with exponential backoff up to 5 minutes."""
    while True:
        doc = await _fallback_queue.get()
        delay = 1.0
        max_delay = 300.0
        while True:
            if db is not None:
                try:
                    await db["transcripts"].insert_one(doc.copy())
                    await push_log(f"[FALLBACK] Queued transcript recovered: {doc.get('interview_id')}")
                    break
                except Exception as e:
                    await push_log(f"[FALLBACK] Retry failed (next in {delay:.0f}s): {e}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
        _fallback_queue.task_done()


async def connect_to_mongo() -> bool:
    global client, db
    try:
        client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        await client.admin.command("ping")
        db = client[DB_NAME]
        msg = f"Connected to MongoDB ({DB_NAME})"
        await push_log(msg)
        return True
    except ServerSelectionTimeoutError:
        msg = "MongoDB connection failed: server not reachable"
        await push_log(msg)
        return False
    except Exception as e:
        msg = f"MongoDB connection error: {e}"
        await push_log(msg)
        return False


async def disconnect_from_mongo() -> None:
    global client, db
    try:
        if client is not None:
            client.close()
            client = None
            db = None
            msg = "MongoDB connection closed"
            await push_log(msg)
    except Exception as e:
        msg = f"MongoDB disconnect error: {e}"
        await push_log(msg)


def get_db():
    """Return the current Motor database instance (None if not connected)."""
    return db


async def download_audio_from_gridfs(audio_id: str) -> bytes | None:
    """
    Download PCM audio bytes from GridFS by file ObjectId string.
    Returns None on any error.
    """
    if db is None:
        return None
    try:
        bucket = AsyncIOMotorGridFSBucket(db)
        stream = await bucket.open_download_stream(ObjectId(str(audio_id)))
        return await stream.read()
    except Exception as e:
        msg = f"GridFS download error (id={audio_id}): {e}"
        print(msg)
        return None


async def insert_transcript(interview_id: str, speaker: str, text: str) -> bool:
    """
    Insert one candidate caption into interviews.transcripts.
    Retries up to 3 times with 1s backoff on transient failures.

    Schema matches what Main-Agent's change stream expects:
      { interview_id, speaker="candidate", text, audio_url=null, timestamp }
    """
    import asyncio as _asyncio
    if db is None:
        return False

    doc = {
        "interview_id": interview_id,
        "speaker": speaker,
        "text": text,
        "audio_url": None,
        "timestamp": datetime.utcnow(),
    }

    for attempt in range(1, 4):
        try:
            result = await db["transcripts"].insert_one(doc.copy())
            msg = f"Transcript inserted (id={result.inserted_id})"
            await push_log(msg)
            return True
        except Exception as e:
            if attempt == 3:
                msg = f"Transcript insert FAILED after 3 attempts — queuing for retry: {e}"
                await push_log(msg)
                # 9.1: Queue for background retry instead of silently losing data
                await _fallback_queue.put(doc)
                await start_fallback_retry_task()
                return False
            msg = f"Transcript insert error (attempt {attempt}/3), retrying in 1s: {e}"
            await push_log(msg)
            await _asyncio.sleep(1)

    return False
