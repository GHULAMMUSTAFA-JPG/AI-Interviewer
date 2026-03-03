"""In-memory interview context caching using cachetools"""

from dataclasses import dataclass
from typing import List
from datetime import datetime
from bson import ObjectId
import asyncio
import structlog

from src.config.performance import interview_cache, MAX_CONVERSATION_HISTORY
from src.database.connection import db

logger = structlog.get_logger()


@dataclass
class InterviewContext:
    """Cached interview context (loaded once per interview)"""
    interview_id: str
    candidate_name: str
    candidate_cv: str          # Full CV (800 tokens)
    job_summary: str           # Optimized JD (300 tokens)
    company_summary: str       # Brief company (50 tokens)
    phase: str
    turn_count: int
    conversation_history: List[str]
    current_summary: str
    started_at: datetime

    def update_conversation(self, message: str):
        """Add message to history, keep last N only"""
        self.conversation_history.append(message)
        if len(self.conversation_history) > MAX_CONVERSATION_HISTORY:
            self.conversation_history = self.conversation_history[-MAX_CONVERSATION_HISTORY:]


async def load_interview_context(interview_id: str) -> InterviewContext:
    """Load context ONCE and cache in memory"""

    # Check cache first (0ms if hit)
    if interview_id in interview_cache:
        logger.debug("cache_hit", interview_id=interview_id)
        return interview_cache[interview_id]

    # Cache miss - load from MongoDB (70-140ms, happens ONCE)
    logger.info("cache_miss_loading", interview_id=interview_id)

    # Load interview document
    interview = await db.interviews.find_one({"_id": ObjectId(interview_id)})
    if not interview:
        raise ValueError(f"Interview not found: {interview_id}")

    # Parallel queries (faster than sequential)
    candidate, job, company, recent_msgs = await asyncio.gather(
        db.candidates.find_one({"_id": interview["candidate_id"]}),
        db.jobs.find_one({"_id": interview["job_id"]}),
        db.companies.find_one({"_id": interview["company_id"]}),
        db.transcripts.find(
            {"interview_id": ObjectId(interview_id)}
        ).sort("timestamp", -1).limit(MAX_CONVERSATION_HISTORY).to_list(MAX_CONVERSATION_HISTORY)
    )

    # Build optimized job summary (300 tokens instead of 600)
    job_summary = f"""Role: {job['title']} at {company['name']}
Required: {', '.join(job.get('required_skills', [])[:10])}
Responsibilities: {job.get('responsibilities_summary', job.get('description', '')[:200])}"""

    # Brief company summary (50 tokens)
    company_summary = f"{company['name']} - {company.get('industry', 'Technology')}, {company.get('size_description', 'Growing company')}"

    # Build context
    context = InterviewContext(
        interview_id=interview_id,
        candidate_name=candidate["name"],
        candidate_cv=candidate["cv_text"],              # FULL 800 tokens
        job_summary=job_summary,                        # OPTIMIZED 300 tokens
        company_summary=company_summary,                # BRIEF 50 tokens
        phase=interview["phase"],
        turn_count=interview["turn_count"],
        conversation_history=[m["text"] for m in reversed(recent_msgs)],
        current_summary=interview.get("current_summary", ""),
        started_at=interview["started_at"]
    )

    # Cache it (TTL = 2 hours, auto-cleanup)
    interview_cache[interview_id] = context

    logger.info(
        "context_cached",
        interview_id=interview_id,
        cache_size=len(interview_cache)
    )

    return context


def clear_interview_cache(interview_id: str):
    """Clear cache when interview ends"""
    if interview_id in interview_cache:
        del interview_cache[interview_id]
        logger.info("cache_cleared", interview_id=interview_id)


def get_cache_stats():
    """Get cache statistics"""
    return {
        "size": len(interview_cache),
        "maxsize": interview_cache.maxsize,
        "ttl": interview_cache.ttl
    }
