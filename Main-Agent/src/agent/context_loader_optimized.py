"""
Optimized context loader with in-memory caching.

Flat schema: all interview data (candidate_cv, job_description, company_info)
lives directly in the interviews.interviews document keyed by interview_id
(UUID string), NOT by MongoDB ObjectId.

Performance improvements:
- First load: 50-100ms (MongoDB queries)
- Subsequent loads: 0-5ms (cache hit)
"""
import asyncio
import time
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
import structlog

from src.agent.models import InterviewContext
from src.config import interview_cache, MAX_CONVERSATION_HISTORY
from src.exceptions import DocumentNotFoundError, DatabaseException

logger = structlog.get_logger()


async def load_interview_context(
    db: AsyncIOMotorDatabase,
    transcript_id: str
) -> InterviewContext:
    """
    Load interview context with in-memory caching.

    The transcript's _id is a MongoDB ObjectId string.
    The transcript's interview_id field is a UUID string.
    The interview document is looked up by {"interview_id": <uuid>}.

    Static data (CV, JD, company) is cached per interview_id.
    Dynamic data (phase, turn_count, recent messages) is loaded fresh each time.
    """
    start_time = time.perf_counter()

    try:
        # 1. Fetch the transcript that triggered this (its _id IS an ObjectId)
        transcript = await db.transcripts.find_one({"_id": ObjectId(transcript_id)})
        if not transcript:
            raise DocumentNotFoundError(f"Transcript not found: {transcript_id}")

        interview_id = str(transcript["interview_id"])
        latest_message = transcript["text"]

        # 2. Check cache for static context (CV, JD, company)
        cache_key = f"context_{interview_id}"

        if cache_key in interview_cache and "interview_id" in interview_cache[cache_key]:
            # CACHE HIT — refresh only dynamic fields
            # Guard: pre-warm in main.py stores partial data (JD/CV/company only).
            # If interview_id is missing the entry is incomplete — fall through to
            # a full load which will overwrite it with the complete context.
            cached_context = interview_cache[cache_key]
            logger.debug("context_cache_hit", interview_id=interview_id)

            # Parallel fetch: interview state + recent messages (saves ~30-50ms vs sequential)
            interview, raw_messages = await asyncio.gather(
                db.interviews.find_one(
                    {"interview_id": interview_id},
                    projection={"phase": 1, "turn_count": 1, "status": 1,
                                "conversation_summary": 1, "phase_turn_count": 1}
                ),
                db.transcripts.find(
                    {"interview_id": interview_id}
                ).sort("timestamp", -1).limit(MAX_CONVERSATION_HISTORY).to_list(MAX_CONVERSATION_HISTORY),
            )
            if not interview:
                raise DocumentNotFoundError(f"Interview not found: {interview_id}")

            raw_messages.reverse()
            recent_messages = [
                {
                    "speaker": msg["speaker"],
                    "text": msg["text"],
                    "timestamp": msg["timestamp"].isoformat()
                    if isinstance(msg.get("timestamp"), datetime)
                    else msg.get("timestamp"),
                }
                for msg in raw_messages
            ]

            context = InterviewContext(
                **cached_context,
                phase=interview.get("phase", "INTRO"),
                turn_count=interview.get("turn_count", 0),
                status=interview.get("status", "in_progress"),
                conversation_summary=interview.get("conversation_summary", ""),
                phase_turn_count=interview.get("phase_turn_count", 0),
                recent_messages=recent_messages,
                latest_message=latest_message,
            )

            latency_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "context_loaded_from_cache",
                interview_id=interview_id,
                latency_ms=round(latency_ms, 2),
                phase=context.phase,
                turn=context.turn_count,
            )
            return context

        # CACHE MISS — load everything from MongoDB
        logger.info("context_cache_miss_loading", interview_id=interview_id)

        # 3. Load interview document (flat schema — CV/JD/company all here)
        interview = await db.interviews.find_one({"interview_id": interview_id})
        if not interview:
            raise DocumentNotFoundError(f"Interview not found: {interview_id}")

        # 4. Load recent messages
        recent_messages_cursor = db.transcripts.find(
            {"interview_id": interview_id}
        ).sort("timestamp", -1).limit(MAX_CONVERSATION_HISTORY)
        recent_messages = await recent_messages_cursor.to_list(length=MAX_CONVERSATION_HISTORY)
        recent_messages.reverse()

        recent_messages = [
            {
                "speaker": msg["speaker"],
                "text": msg["text"],
                "timestamp": msg["timestamp"].isoformat()
                if isinstance(msg.get("timestamp"), datetime)
                else msg.get("timestamp"),
            }
            for msg in recent_messages
        ]

        # 5. Cache STATIC fields only (CV, JD, company, candidate).
        # Dynamic fields (phase, turn_count, status, conversation_summary) are NOT
        # cached — they're fetched fresh on every call to avoid stale reads, and
        # passed explicitly so **cached_context never conflicts with keyword args.
        cached_context = {
            "interview_id": interview.get("interview_id", str(interview.get("_id", ""))),
            "candidate_id": "",  # not used in flat schema
            "job_id": "",        # not used in flat schema
            "started_at": interview.get("started_at"),
            "candidate_name": interview.get("candidate_name", "Candidate"),
            "candidate_cv": interview.get("candidate_cv", ""),
            "job_description": interview.get("job_description", ""),
            "company_info": interview.get("company_info", ""),
            "required_skills": [],
            "experience_level": "",
            "estimated_context_tokens": 0,
        }
        interview_cache[cache_key] = cached_context

        # 6. Build complete InterviewContext (static from cache + dynamic from DB)
        context = InterviewContext(
            **cached_context,
            phase=interview.get("phase", "INTRO"),
            turn_count=interview.get("turn_count", 0),
            status=interview.get("status", "in_progress"),
            conversation_summary=interview.get("conversation_summary", ""),
            phase_turn_count=interview.get("phase_turn_count", 0),
            recent_messages=recent_messages,
            latest_message=latest_message,
        )

        latency_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "context_loaded_and_cached",
            interview_id=interview_id,
            latency_ms=round(latency_ms, 2),
            phase=context.phase,
            turn=context.turn_count,
        )
        return context

    except DocumentNotFoundError:
        raise

    except Exception as e:
        logger.error("context_loading_error", error=str(e), error_type=type(e).__name__, exc_info=True)
        raise DatabaseException(f"Failed to load context: {e}")
