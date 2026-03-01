"""
Meeting-Bot persistent service.

Watches interviews.interviews for new inserts (status="in_progress")
and spawns a browser bot for each meeting URL.
"""

import asyncio
import os
import signal
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

from bot import join_meeting
from logger import push_log

load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
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

    if not meeting_url:
        msg = f"No meeting_url in interview {interview_id} — skipping"
        print(msg); await push_log(msg)
        return

    msg = f"Starting bot for interview_id={interview_id}  url={meeting_url}"
    print(msg); await push_log(msg)

    await join_meeting(
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

    pipeline = [{"$match": {"operationType": "insert"}}]

    async with db.interviews.watch(pipeline) as stream:
        async for change in stream:
            if shutdown_event.is_set():
                break

            interview = change["fullDocument"]
            status = interview.get("status", "")

            # Only act on in_progress interviews
            if status != "in_progress":
                continue

            asyncio.create_task(run_bot(interview))

    client.close()
    msg = "Meeting-Bot stopped"
    print(msg); await push_log(msg)


if __name__ == "__main__":
    asyncio.run(main())
