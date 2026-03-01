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
        print(msg); await push_log(msg)
        return True
    except ServerSelectionTimeoutError:
        msg = "MongoDB connection failed: server not reachable"
        print(msg); await push_log(msg)
        return False
    except Exception as e:
        msg = f"MongoDB connection error: {e}"
        print(msg); await push_log(msg)
        return False


async def disconnect_from_mongo() -> None:
    global client, db
    try:
        if client:
            client.close()
            client = None
            db = None
            msg = "MongoDB connection closed"
            print(msg); await push_log(msg)
    except Exception as e:
        msg = f"MongoDB disconnect error: {e}"
        print(msg); await push_log(msg)


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

    Schema matches what Main-Agent's change stream expects:
      { interview_id, speaker="candidate", text, audio_url=null, timestamp }
    """
    if db is None:
        return False
    try:
        doc = {
            "interview_id": interview_id,
            "speaker": speaker,
            "text": text,
            "audio_url": None,
            "timestamp": datetime.utcnow(),
        }
        result = await db["transcripts"].insert_one(doc)
        msg = f"Transcript inserted (id={result.inserted_id})"
        print(msg); await push_log(msg)
        return True
    except Exception as e:
        msg = f"Transcript insert error: {e}"
        print(msg); await push_log(msg)
        return False
