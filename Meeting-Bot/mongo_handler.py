"""
MongoDB handler for Meeting-Bot.
Writes candidate captions to interviews.transcripts only.
This collection is the shared event bus between all services.
"""

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
            result = await db["transcripts"].insert_one(doc)
            msg = f"Transcript inserted (id={result.inserted_id})"
            await push_log(msg)
            return True
        except Exception as e:
            if attempt == 3:
                msg = f"Transcript insert FAILED after 3 attempts: {e}"
                await push_log(msg)
                return False
            msg = f"Transcript insert error (attempt {attempt}/3), retrying in 1s: {e}"
            await push_log(msg)
            await _asyncio.sleep(1)

    return False
