"""
main.py — Meeting-Bot Entry Point (Web Speech API Mode)

Watches interviews.interviews for new in_progress inserts
and spawns a browser bot for each meeting URL.

Redis Integration:
- Publishes bot status changes to Redis
- Sends heartbeats every 10s
- Enables real-time UI updates
"""

import asyncio
import os
import signal
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

from join_meeting import join_meeting_and_transcribe
from logger import push_log
from redis_client import get_redis, RedisState, close_redis

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
        msg = f"No meeting_url — skipping"
        print(msg); await push_log(msg, interview_id=interview_id)
        return

    msg = f"Starting bot  url={meeting_url}"
    print(msg); await push_log(msg, interview_id=interview_id)

    # Update Redis status
    try:
        redis = await get_redis()
        state = RedisState(redis)
        await state.set_bot_status(interview_id, "joining", meeting_url=meeting_url)
        await state.set_meeting_status(interview_id, "waiting", url=meeting_url)
    except Exception as e:
        print(f"⚠️  Redis update failed: {e}")

    await join_meeting_and_transcribe(
        url=meeting_url,
        email=BOT_EMAIL,
        interview_id=interview_id,
    )


async def _run_single_interview(interview_id: str) -> None:
    """
    Single-interview mode — used when INTERVIEW_ID env var is set.

    Reads the interview document from MongoDB, runs the bot, then exits.
    The orchestrator spawns one container per interview, so the container
    lifecycle maps 1:1 to the interview lifecycle.
    """
    msg = f"[SINGLE] Starting single-interview mode for {interview_id}"
    print(msg); await push_log(msg)

    client = await _connect_with_retry()
    db = client[DB_NAME]

    # Wait for interview document (it may not be committed yet when container starts)
    interview = None
    for _ in range(20):
        interview = await db["interviews"].find_one({"interview_id": interview_id})
        if interview:
            break
        await asyncio.sleep(1)

    if not interview:
        msg = f"[SINGLE] Interview {interview_id} not found in MongoDB — exiting"
        print(msg); await push_log(msg)
        return

    # Signal shared TTS service to skip this interview (local TTS handles it)
    try:
        redis = await get_redis()
        await redis.setex(f"tts:{interview_id}:local", 7200, "1")  # 2h TTL
        msg = f"[SINGLE] Marked tts:{interview_id}:local in Redis"
        print(msg); await push_log(msg)
    except Exception as e:
        print(f"⚠️  Redis flag failed (continuing): {e}")

    try:
        await run_bot(interview)
    finally:
        # Mark any unplayed agent transcripts so shared TTS (which has no PulseAudio)
        # never synthesizes them. The tts:local Redis flag expires via its 2h TTL —
        # we do NOT delete it here, because deleting it would expose these docs to
        # shared TTS before they're all marked.
        try:
            await db["transcripts"].update_many(
                {"interview_id": interview_id, "audio_url": None, "speaker": "agent"},
                {"$set": {"audio_url": "bot_container_exited"}},
            )
        except Exception as e:
            print(f"[SINGLE] Failed to mark remaining transcripts: {e}")
        await close_redis()


async def main() -> None:
    # Single-interview mode: INTERVIEW_ID set by orchestrator when spawning container
    single_interview_id = os.getenv("INTERVIEW_ID", "").strip()
    if single_interview_id:
        await _run_single_interview(single_interview_id)
        return

    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except (NotImplementedError, OSError):
            signal.signal(sig, lambda _s, _f: shutdown_event.set())

    # Connect to Redis
    try:
        redis = await get_redis()
        state = RedisState(redis)
        print("✅ Redis connected and ready")
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")
        # Continue without Redis (degraded mode)
        state = None

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
        except Exception as exc:
            print(f"❌ Bot task error for {interview_id}: {exc}")
        finally:
            _active.discard(interview_id)
            if state:
                try:
                    await state.set_bot_status(interview_id, "completed")
                    await state.set_meeting_status(interview_id, "ended")
                except Exception:
                    pass

    # Change stream with auto-reconnect — if MongoDB drops and reconnects,
    # the watcher resumes rather than dying permanently.
    retry_delay = 2.0
    while not shutdown_event.is_set():
        try:
            async with db["interviews"].watch(
                [{"$match": {"operationType": "insert"}}],
                full_document="updateLookup",
            ) as stream:
                retry_delay = 2.0  # reset on successful open
                msg = "👀 Change stream open — watching for new interviews"
                print(msg); await push_log(msg)

                async for change in stream:
                    if shutdown_event.is_set():
                        break

                    full_doc = change.get("fullDocument") or {}
                    if full_doc.get("status") != "in_progress":
                        continue

                    interview_id = full_doc.get("interview_id", "unknown")
                    if interview_id in _active:
                        msg = f"Bot already running for {interview_id} — skipping duplicate"
                        print(msg); await push_log(msg)
                        continue

                    _active.add(interview_id)
                    asyncio.create_task(_run_and_release(full_doc))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            if shutdown_event.is_set():
                break
            msg = f"⚠️ Change stream error (retry in {retry_delay}s): {exc}"
            print(msg); await push_log(msg)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)

            # Reconnect MongoDB client on persistent errors
            try:
                await client.admin.command("ping")
            except Exception:
                msg = "🔄 MongoDB connection lost — reconnecting..."
                print(msg); await push_log(msg)
                try:
                    client = await _connect_with_retry(max_attempts=5)
                    db = client[DB_NAME]
                    msg = "✅ MongoDB reconnected"
                    print(msg); await push_log(msg)
                except Exception as reconnect_exc:
                    msg = f"❌ MongoDB reconnect failed: {reconnect_exc}"
                    print(msg); await push_log(msg)

    # Graceful shutdown — wait for active bots to finish
    if _active:
        msg = f"Shutdown: waiting for {len(_active)} active bot(s) to finish..."
        print(msg); await push_log(msg)

    await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
