"""
Local TTS listener for single-interview bot containers.

Watches interviews.transcripts for agent messages belonging to THIS container's
interview, synthesizes with edge-tts → mpg123 → pacat (local PulseAudio).

Runs as a background process started by entrypoint.sh when INTERVIEW_ID is set.
Mirrors the pipeline used by the shared TTS service so audio quality is identical.
"""
import asyncio
import logging
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  TTS-LOCAL  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://host.docker.internal:27017/?replicaSet=rs0")
REDIS_URI = os.getenv("REDIS_URI", "redis://localhost:6379/0")
INTERVIEW_ID = os.environ["INTERVIEW_ID"]
VOICE = os.getenv("EDGE_TTS_VOICE", "en-US-AndrewNeural")
VIRTUAL_MIC = os.getenv("VIRTUAL_MIC", "virtual_mic")
DB_NAME = "interviews"

_MAX_RETRIES = 3
_interrupt_event: asyncio.Event = asyncio.Event()  # set when interrupt signal arrives

_redis_client = None  # shared Redis client for status publishing


async def _get_redis():
    global _redis_client
    if _redis_client is None:
        from redis.asyncio import Redis
        _redis_client = Redis.from_url(REDIS_URI, decode_responses=True)
    return _redis_client


async def _publish_tts_status(status: str, interrupted: bool = False, duration_ms: int = 0) -> None:
    """Publish TTS status to Redis so the echo guard can gate/ungate the browser STT."""
    import json
    try:
        r = await _get_redis()
        payload = json.dumps({"status": status, "interrupted": interrupted})
        await r.publish(f"tts:{INTERVIEW_ID}:status_events", payload)
        if duration_ms > 0:
            await r.set(f"tts:{INTERVIEW_ID}:audio_duration_ms", str(duration_ms), ex=60)
    except Exception as e:
        log.warning(f"Redis status publish failed (non-fatal): {e}")


async def _fetch_mp3(text: str) -> bytes:
    import edge_tts

    last_exc: Exception = RuntimeError("no attempts")
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            communicate = edge_tts.Communicate(text=text, voice=VOICE, rate="+15%", volume="+0%")
            chunks = [c["data"] async for c in communicate.stream() if c["type"] == "audio"]
            if not chunks:
                raise RuntimeError("edge-tts returned no audio data")
            return b"".join(chunks)
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(2.0 * attempt)
    raise last_exc


async def _play(mp3_data: bytes) -> tuple[bool, int]:
    """Decode MP3 → PCM via mpg123, then stream PCM to pacat → virtual_mic.
    Returns (played_fully, duration_ms).
    """
    _interrupt_event.clear()

    # Step 1: MP3 → raw PCM s16le 22050Hz mono
    mpg = await asyncio.create_subprocess_exec(
        "mpg123", "-q", "-r", "22050", "-m", "-s", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    pcm_data, _ = await mpg.communicate(input=mp3_data)
    if mpg.returncode != 0 or not pcm_data:
        raise RuntimeError(f"mpg123 decode failed (rc={mpg.returncode})")

    # Duration: s16le 22050Hz mono = 2 bytes/sample
    duration_ms = int(len(pcm_data) / (22050 * 2) * 1000)

    # Step 2: PCM → pacat → virtual_mic (local PulseAudio)
    pacat = await asyncio.create_subprocess_exec(
        "pacat", "--playback", f"--device={VIRTUAL_MIC}",
        "--format=s16le", "--rate=22050", "--channels=1",
        stdin=asyncio.subprocess.PIPE,
    )
    pacat.stdin.write(pcm_data)
    pacat.stdin.close()

    # Wait for playback to finish OR interrupt signal — whichever comes first
    done, _ = await asyncio.wait(
        [asyncio.create_task(pacat.wait()),
         asyncio.create_task(_interrupt_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    if _interrupt_event.is_set():
        try:
            pacat.terminate()
        except Exception:
            pass
        log.info("Playback interrupted by candidate speech")
        return False, duration_ms
    return True, duration_ms


async def _watch_interrupt() -> None:
    """Subscribe to Redis interrupt_signal channel and set _interrupt_event."""
    try:
        from redis.asyncio import Redis
        r = Redis.from_url(REDIS_URI, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe(f"tts:{INTERVIEW_ID}:interrupt_signal")
        async for message in pubsub.listen():
            if message.get("type") == "message":
                log.info("Interrupt signal received — stopping TTS")
                _interrupt_event.set()
    except Exception as e:
        log.warning(f"Interrupt watcher error: {e}")


async def _mark_played(db, doc_id) -> None:
    from bson import ObjectId
    try:
        await db.transcripts.update_one(
            {"_id": doc_id},
            {"$set": {"audio_url": "local_tts_played"}},
        )
    except Exception as e:
        log.warning(f"mark_played failed: {e}")


async def main() -> None:
    log.info(f"Starting local TTS listener for interview {INTERVIEW_ID}")

    # Run interrupt watcher concurrently so we can stop pacat mid-playback
    asyncio.create_task(_watch_interrupt())

    client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=10000)
    db = client[DB_NAME]

    # Backfill: play any agent messages already in DB that haven't been played yet.
    # This covers the greeting inserted before this container fully started.
    async for doc in db.transcripts.find(
        {"interview_id": INTERVIEW_ID, "speaker": "agent", "audio_url": None},
        sort=[("timestamp", 1)],
    ):
        await _handle(db, doc)

    # Watch for new agent messages
    pipeline = [{"$match": {
        "operationType": "insert",
        "fullDocument.speaker": "agent",
        "fullDocument.interview_id": INTERVIEW_ID,
    }}]

    retry_delay = 2.0
    while True:
        try:
            async with db.transcripts.watch(pipeline, full_document="updateLookup") as stream:
                async for change in stream:
                    doc = change.get("fullDocument") or {}
                    if doc.get("audio_url") is not None:
                        continue  # already played by backfill or someone else
                    await _handle(db, doc)
        except Exception as exc:
            log.error(f"Change stream error — restarting in {retry_delay}s: {exc}")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)


async def _handle(db, doc: dict) -> None:
    text = (doc.get("text") or "").strip()
    if not text:
        return

    # Skip if already played (race guard for backfill vs. change stream overlap)
    fresh = await db.transcripts.find_one({"_id": doc["_id"]}, {"audio_url": 1})
    if fresh and fresh.get("audio_url") is not None:
        return

    log.info(f"Speaking: {text[:70]}...")
    try:
        mp3 = await _fetch_mp3(text)
        # Notify echo guard: bot is about to speak — gate the browser STT
        await _publish_tts_status("speaking")
        played_fully, duration_ms = await _play(mp3)
        log.info(f"Done speaking ({len(mp3):,} bytes MP3, {duration_ms}ms)")
        # Notify echo guard: bot finished — ungate STT, pass duration for echo gate
        await _publish_tts_status("idle", interrupted=not played_fully, duration_ms=duration_ms)
    except Exception as e:
        log.error(f"TTS error: {e}")
        # Ensure STT is always ungated even on error
        await _publish_tts_status("idle")
    finally:
        await _mark_played(db, doc["_id"])


if __name__ == "__main__":
    asyncio.run(main())
