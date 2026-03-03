"""
Context loader - loads interview context from MongoDB.
Fetches CV, JD, company info via references (not duplicates).
"""
import asyncio
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId

from src.agent.models import InterviewContext
from src.config import logger
from src.exceptions import DocumentNotFoundError, DatabaseException


async def load_interview_context(
    db: AsyncIOMotorDatabase,
    transcript_id: str
) -> InterviewContext:
    """
    Load all context needed to generate agent response.

    Args:
        db: MongoDB database instance
        transcript_id: ObjectId of the transcript that triggered this

    Returns:
        InterviewContext with all loaded data

    Raises:
        DocumentNotFoundError: If required documents not found
        DatabaseException: If database query fails
    """
    try:
        # 1. Get the transcript that triggered this
        transcript = await db.transcripts.find_one({"_id": ObjectId(transcript_id)})
        if not transcript:
            raise DocumentNotFoundError(f"Transcript not found: {transcript_id}")

        interview_id = transcript["interview_id"]
        latest_message = transcript["text"]

        # 2. Get interview metadata
        interview = await db.interviews.find_one(
            {"_id": interview_id},
            projection={
                "candidate_id": 1,
                "job_id": 1,
                "phase": 1,
                "turn_count": 1,
                "status": 1,
                "conversation_summary": 1,
                "started_at": 1
            }
        )

        if not interview:
            raise DocumentNotFoundError(f"Interview not found: {interview_id}")

        # 3. Parallel fetch: candidate and job (saves ~50ms)
        candidate, job = await asyncio.gather(
            db.candidates.find_one(
                {"_id": interview["candidate_id"]},
                projection={"name": 1, "cv_text": 1}
            ),
            db.jobs.find_one(
                {"_id": interview["job_id"]},
                projection={"description": 1, "company_id": 1, "required_skills": 1, "experience_level": 1}
            )
        )

        if not candidate:
            raise DocumentNotFoundError(f"Candidate not found: {interview['candidate_id']}")

        if not job:
            raise DocumentNotFoundError(f"Job not found: {interview['job_id']}")

        # 4. Get company info (now that we have company_id from job)
        company = await db.companies.find_one(
            {"_id": job["company_id"]},
            projection={"info": 1}
        )

        if not company:
            raise DocumentNotFoundError(f"Company not found: {job['company_id']}")

        # 5. Get last 6 messages (3 candidate + 3 agent)
        recent_messages_cursor = db.transcripts.find(
            {"interview_id": interview_id}
        ).sort("timestamp", -1).limit(6)

        recent_messages = await recent_messages_cursor.to_list(length=6)
        recent_messages.reverse()  # Oldest first for chronological order

        # Convert to simple dict format
        recent_messages = [
            {
                "speaker": msg["speaker"],
                "text": msg["text"],
                "timestamp": msg["timestamp"].isoformat() if isinstance(msg.get("timestamp"), datetime) else msg.get("timestamp")
            }
            for msg in recent_messages
        ]

        # 6. Build InterviewContext
        context = InterviewContext(
            interview_id=str(interview_id),
            candidate_id=str(interview["candidate_id"]),
            job_id=str(interview["job_id"]),
            phase=interview["phase"],
            turn_count=interview["turn_count"],
            status=interview["status"],
            started_at=interview.get("started_at"),
            conversation_summary=interview.get("conversation_summary", ""),
            candidate_name=candidate.get("name", "Candidate"),
            candidate_cv=candidate["cv_text"],
            job_description=job["description"],
            company_info=company["info"],
            required_skills=job.get("required_skills", []),
            experience_level=job.get("experience_level", ""),
            recent_messages=recent_messages,
            latest_message=latest_message
        )

        logger.info(f"Loaded context for interview {interview_id}, phase: {context.phase}, turn: {context.turn_count}")

        return context

    except DocumentNotFoundError:
        raise

    except Exception as e:
        logger.error(f"Error loading interview context: {e}")
        raise DatabaseException(f"Failed to load context: {e}")
