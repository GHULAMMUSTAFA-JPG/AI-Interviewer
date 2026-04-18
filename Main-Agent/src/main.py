"""
AI Interview Agent — MongoDB change stream listener.

Production features:
- MongoDB connect with exponential-backoff retry
- Change stream resume token (survives restarts without replaying old events)
- Graceful shutdown: waits for in-flight messages before exiting
- Structured JSON logging via structlog
- Greeting watcher: inserts opening message when a new interview is created
"""
import sys
import signal
import asyncio
import structlog
import logging
from datetime import datetime
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Human-readable log format: "HH:MM:SS  LEVEL  event  key=val ..."
def _plain_renderer(logger, method, event_dict):
    ts    = event_dict.pop("timestamp", "")
    level = event_dict.pop("level", method).upper()[:4]
    event = event_dict.pop("event", "")
    extras = "  ".join(f"{k}={v}" for k, v in event_dict.items() if v is not None)
    return f"{ts}  {level:<4}  {event}" + (f"  {extras}" if extras else "")

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        _plain_renderer,
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

from src.config import Config, MongoDB, logger
from src.agent.pipeline import process_candidate_message

logger_struct = structlog.get_logger()

# -------------------------------------------------------
# In-flight task registry — used by graceful shutdown
# -------------------------------------------------------
_pending_tasks: set[asyncio.Task] = set()

# Per-interview locks — ensures only one LLM call runs at a time per interview.
# Without this, two candidate messages arriving within seconds each spawn their
# own asyncio task → two concurrent LLM calls → two TTS responses queued → bot
# talks non-stop and doesn't let the candidate speak.
_interview_locks: dict[str, asyncio.Lock] = {}



# -------------------------------------------------------
# Resume token helpers
# -------------------------------------------------------

async def _load_resume_token(db) -> dict | None:
    """Return the persisted change stream resume token, or None on first run."""
    state = await db.agent_state.find_one({"_id": "resume_token"})
    return state["token"] if state else None


async def _save_resume_token(db, token: dict) -> None:
    """Persist the latest resume token so the agent can resume after restart."""
    await db.agent_state.update_one(
        {"_id": "resume_token"},
        {"$set": {"token": token}},
        upsert=True
    )


# -------------------------------------------------------
# MongoDB connect with retry
# -------------------------------------------------------

async def _connect_with_retry(max_attempts: int = 10) -> None:
    """
    Try to connect to MongoDB with exponential backoff.
    Useful when the agent starts before MongoDB is fully ready.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            await MongoDB.connect()
            return
        except Exception as exc:
            if attempt == max_attempts:
                raise
            wait = min(2 ** attempt, 30)   # cap at 30s
            logger.warning(
                f"MongoDB not ready (attempt {attempt}/{max_attempts}), "
                f"retrying in {wait}s — {exc}"
            )
            await asyncio.sleep(wait)


# -------------------------------------------------------
# Change stream loop (extracted so retry logic is clean)
# -------------------------------------------------------

async def _watch(db, pipeline: list, resume_token: dict | None,
                 shutdown_event: asyncio.Event) -> None:
    """Open a change stream and process events until shutdown is requested."""
    watch_kwargs = {"resume_after": resume_token} if resume_token else {}

    async with db.transcripts.watch(pipeline, **watch_kwargs) as stream:
        async for change in stream:
            if shutdown_event.is_set():
                break

            transcript = change["fullDocument"]
            transcript_id = str(transcript["_id"])

            # Persist token BEFORE processing — at-most-once delivery.
            # On restart the agent skips this message; it won't duplicate responses.
            await _save_resume_token(db, change["_id"])

            logger.info(f"\n[NEW MESSAGE] Received")
            logger.info(f"   Transcript ID: {transcript_id}")
            logger.info(f"   Message: {transcript['text'][:50]}...")

            interview_id = str(transcript.get("interview_id", "unknown"))
            task = asyncio.create_task(
                _process_with_interview_lock(db, transcript_id, interview_id)
            )
            _pending_tasks.add(task)
            task.add_done_callback(_pending_tasks.discard)


# -------------------------------------------------------
# Main
# -------------------------------------------------------

async def _watch_new_interviews(db, shutdown_event: asyncio.Event) -> None:
    """
    Watch interviews.interviews for bot_status='admitted' updates and then
    insert the opening greeting into interviews.transcripts.

    We wait for Meeting-Bot to set bot_status='admitted' (after the mic is
    confirmed unmuted) rather than on the raw insert.

    NOTE: No resume token - always start from NOW to catch new admissions.
    """
    pipeline = [
        {
            "$match": {
                "operationType": "update",
                "updateDescription.updatedFields.bot_status": "admitted",
            }
        }
    ]

    while not shutdown_event.is_set():
        try:
            async with db.interviews.watch(pipeline, full_document="updateLookup") as stream:
                async for change in stream:
                    if shutdown_event.is_set():
                        return

                    interview = change.get("fullDocument") or {}
                    interview_id = str(interview.get("interview_id", interview.get("_id", "unknown")))

                    logger.info(f"[BOT ADMITTED] {interview_id} — waiting 5 seconds before inserting greeting")

                    # PRE-WARM: Load complete interview context into cache before candidate speaks.
                    # Only store STATIC fields (CV, JD, company). Dynamic fields (phase, turn_count,
                    # status, conversation_summary) are fetched fresh by the context loader — storing
                    # them here causes "got multiple values for keyword argument 'phase'" crash.
                    try:
                        interview_doc = await db.interviews.find_one({"interview_id": interview_id})
                        if interview_doc:
                            from src.config import interview_cache
                            cache_key = f"context_{interview_id}"
                            if cache_key not in interview_cache:
                                interview_cache[cache_key] = {
                                    # Static fields only — NO phase, turn_count, status, conversation_summary
                                    "interview_id": interview_id,
                                    "candidate_id": "",
                                    "job_id": "",
                                    "started_at": interview_doc.get("started_at"),
                                    "candidate_name": "Candidate",
                                    "candidate_cv": interview_doc.get("candidate_cv", ""),
                                    "job_description": interview_doc.get("job_description", ""),
                                    "company_info": interview_doc.get("company_info", ""),
                                    "required_skills": [],
                                    "experience_level": "",
                                    "estimated_context_tokens": 0,
                                }
                                logger.info(f"[CONTEXT PRE-WARMED] {interview_id}")
                    except Exception as ctx_exc:
                        logger.warning(f"[CONTEXT PRE-WARM] Failed for {interview_id}: {ctx_exc}")

                    # Idempotency guard: skip if a greeting was already sent
                    existing = await db.transcripts.find_one({
                        "interview_id": interview_id,
                        "speaker": "agent",
                    })
                    if existing:
                        logger.info(f"[GREETING] Already sent for {interview_id} — skipping duplicate")
                        continue

                    # Wait 10 seconds after bot admission for audio to stabilize.
                    # The bot takes ~5-8s after admission to: set up PulseAudio routing,
                    # inject Web Speech API, establish its WebRTC audio stream to the
                    # meeting, and stabilize volume levels. If we insert the greeting
                    # too early, the TTS audio plays through virtual_mic but Chrome
                    # isn't capturing it yet → nobody hears it.
                    logger.info(f"[GREETING] Waiting 10 seconds for bot audio to stabilize...")
                    await asyncio.sleep(10)

                    try:
                        await db.transcripts.insert_one({
                            "interview_id": interview_id,
                            "speaker": "agent",
                            "text": (
                                "Hello! Welcome to your technical interview today. "
                                "I'm excited to speak with you. "
                                "To start, could you please introduce yourself and tell me about your background?"
                            ),
                            "audio_url": None,
                            "timestamp": datetime.utcnow(),
                        })
                        logger.info(f"[GREETING SENT] {interview_id}")
                    except Exception as insert_exc:
                        logger.error(f"[GREETING] Failed to insert greeting for {interview_id}: {insert_exc}")

        except Exception as exc:
            if shutdown_event.is_set():
                return
            logger.error(f"[GREETING WATCHER] Error — restarting in 2s: {exc}")
            await asyncio.sleep(2)


async def main() -> None:
    shutdown_event = asyncio.Event()

    # Register SIGTERM / SIGINT for graceful shutdown.
    # Works in Docker (Linux); falls back on Windows.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except (NotImplementedError, OSError):
            signal.signal(sig, lambda _s, _f: shutdown_event.set())

    try:
        Config.validate()
        await _connect_with_retry()
        db = MongoDB.get_db()

        # Compound index: every turn does find({interview_id, speaker}) — needs this
        await db.transcripts.create_index(
            [("interview_id", 1), ("speaker", 1), ("timestamp", -1)],
            background=True
        )

        logger.info("AI Interview Agent started!")
        logger.info(f"Listening for candidate messages in {Config.MONGODB_DB}...")
        logger.info(f"Using LLM provider: {Config.LLM_PROVIDER}")

        pipeline = [
            {"$match": {
                "operationType": "insert",
                "fullDocument.speaker": "candidate"
            }}
        ]

        resume_token = await _load_resume_token(db)
        if resume_token:
            logger.info("Resuming change stream from saved token")

        async def _candidate_watcher():
            current_token = resume_token
            retry_delay = 2
            while not shutdown_event.is_set():
                try:
                    await _watch(db, pipeline, current_token, shutdown_event)
                    return  # clean shutdown
                except Exception as exc:
                    if shutdown_event.is_set():
                        return
                    # If the saved token is too old (oplog rotated), clear it
                    if current_token and (
                        "InvalidResumeToken" in type(exc).__name__
                        or "ChangeStreamHistoryLost" in type(exc).__name__
                        or "resume" in str(exc).lower()
                    ):
                        logger.warning(
                            f"Resume token expired — clearing and restarting: {exc}"
                        )
                        await db.agent_state.delete_one({"_id": "resume_token"})
                        current_token = None
                    else:
                        logger.error(
                            f"[CANDIDATE WATCHER] Error — restarting in {retry_delay}s: {exc}"
                        )
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 30)

        # Run both watchers concurrently
        await asyncio.gather(
            _candidate_watcher(),
            _watch_new_interviews(db, shutdown_event),
        )

    except KeyboardInterrupt:
        logger.info("Shutdown requested (KeyboardInterrupt)")

    except Exception as exc:
        logger.error(f"[FATAL] {exc}", exc_info=True)

    finally:
        if _pending_tasks:
            logger.info(f"Waiting for {len(_pending_tasks)} in-flight message(s) to finish (max 30s)...")
            await asyncio.wait(_pending_tasks, timeout=30)
        await MongoDB.disconnect()
        logger.info("Agent stopped")


# -------------------------------------------------------
# Per-message error handler + interview lock wrapper
# -------------------------------------------------------

async def _process_with_interview_lock(db, transcript_id: str, interview_id: str) -> None:
    """
    Acquire the per-interview lock before processing.  This serialises LLM calls
    for the same interview so two near-simultaneous candidate messages can never
    produce two concurrent responses.
    """
    if interview_id not in _interview_locks:
        _interview_locks[interview_id] = asyncio.Lock()
    async with _interview_locks[interview_id]:
        await _process_with_error_handling(db, transcript_id)


async def _process_with_error_handling(db, transcript_id: str) -> None:
    try:
        output = await process_candidate_message(db, transcript_id)
        logger.info(f"[SUCCESS] {output.response_text[:100]}...")
    except Exception as exc:
        logger.error(f"[ERROR] Failed to process {transcript_id}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
