"""
MongoDB Index Setup — run once on first deployment.
Creates indexes for optimal query performance across all services.
"""
import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://host.docker.internal:27017/?directConnection=true")
DB_NAME = os.getenv("MONGODB_DB", "interviews")

async def setup_indexes():
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    # ─── interviews collection ───
    interviews = db["interviews"]
    # Unique index on interview_id (prevent duplicates)
    await interviews.create_index("interview_id", unique=True)
    # Status queries (cleanup service, UI polling)
    await interviews.create_index("status")
    # Started-at queries (duration checks, sorting)
    await interviews.create_index("started_at")
    # Compound index for status + started_at (cleanup service)
    await interviews.create_index([("status", 1), ("started_at", -1)])
    print("✅ interviews indexes: interview_id (unique), status, started_at, status+started_at")

    # ─── transcripts collection ───
    transcripts = db["transcripts"]
    # Interview lookup
    await transcripts.create_index("interview_id")
    # TTS queue: agent transcripts without audio_url
    await transcripts.create_index([("speaker", 1), ("audio_url", 1)])
    # Conversation view: sort by timestamp
    await transcripts.create_index([("interview_id", 1), ("timestamp", 1)])
    # TTS backfill: agent transcripts with no audio
    await transcripts.create_index([("speaker", 1), ("audio_url", 1), ("timestamp", 1)])
    print("✅ transcripts indexes: interview_id, speaker+audio_url, interview_id+timestamp")

    # ─── GridFS audio collections ───
    # audio.files and audio.chunks are managed by GridFS automatically
    # No additional indexes needed

    print("✅ All MongoDB indexes created successfully")


if __name__ == "__main__":
    asyncio.run(setup_indexes())
