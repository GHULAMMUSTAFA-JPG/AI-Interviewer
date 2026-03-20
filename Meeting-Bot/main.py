"""
main.py — Meeting-Bot Entry Point (Web Speech API Mode)

Watches interviews.interviews for new in_progress inserts
and spawns a browser bot for each meeting URL.
"""

import asyncio
import os
import signal
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

from bot import join_meeting_and_transcribe
from logger import push_log

load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://host.docker.internal:27017/?replicaSet=rs0")
BOT_EMAIL = os.getenv("BOT_EMAIL", "bot@example.com")
DB_NAME = "interviews"


async def _connect_with_retry(max_attempts: int = 10) -> AsyncIOMotorClient:
    for attempt in range(1, max_attempts + 1):
        try:
            client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
            await client.admin.command("ping")
            msg = f"Connected to MongoDB ({DB_NAME})"
            print(msg); await push_log(msg)
            return client
        except Exception as exc:
            if attempt == max_attempts:
                raise
            wait = min(2 ** attempt, 30)
            msg = f"MongoDB not ready (attempt {attempt}/{max_attempts}), retrying in {wait}s: {exc}"
            print(msg); await push_log(msg)
            await asyncio.sleep(wait)


async def run_bot(interview: dict) -> None:
    """Spawn a browser bot for one interview."""
    interview_id = str(interview.get("interview_id", interview.get("_id", "unknown")))
    meeting_url = interview.get("meeting_url", "")

    # Ensure URL has https:// prefix
    if meeting_url and not meeting_url.startswith("http"):
        meeting_url = "https://" + meeting_url.lstrip("/")
    
    # Also strip any trailing slashes
    meeting_url = meeting_url.rstrip("/")

    if not meeting_url:
        msg = f"No meeting_url in interview {interview_id} — skipping"
        print(msg); await push_log(msg)
        return

    msg = f"Starting bot for interview_id={interview_id}  url={meeting_url}"
    print(msg); await push_log(msg)

    await join_meeting_and_transcribe(
        url=meeting_url,
        email=BOT_EMAIL,
        interview_id=interview_id,
    )


async def main() -> None:
    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except (NotImplementedError, OSError):
            signal.signal(sig, lambda _s, _f: shutdown_event.set())

    client = await _connect_with_retry()
    db = client[DB_NAME]

    msg = "Meeting-Bot started — watching interviews.interviews for new meetings"
    print(msg); await push_log(msg)

    # Tracks interview_ids that currently have an active bot task.
    _active: set[str] = set()

    async def _run_and_release(interview: dict) -> None:
        interview_id = str(interview.get("interview_id", interview.get("_id", "unknown")))
        try:
            await run_bot(interview)
        finally:
            _active.discard(interview_id)

    async with db["interviews"].watch(
        [{"$match": {"operationType": "insert"}}],
        full_document="updateLookup",
    ) as stream:
        async for change in stream:
            full_doc = change.get("fullDocument") or {}
            status = full_doc.get("status", "")

            if status != "in_progress":
                continue

            interview_id = full_doc.get("interview_id", "unknown")

            if interview_id in _active:
                msg = f"Bot already running for {interview_id} — skipping duplicate"
                print(msg); await push_log(msg)
                continue

            _active.add(interview_id)
            asyncio.create_task(_run_and_release(full_doc))

            if shutdown_event.is_set():
                break

    for interview_id in list(_active):
        msg = f"Waiting for bot {interview_id} to finish..."
        print(msg); await push_log(msg)

    await shutdown_event.wait()

    for interview_id in list(_active):
        msg = f"Shutting down bot for {interview_id}..."
        print(msg); await push_log(msg)

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
