"""
MongoDB database setup script.

Creates collections, indexes, and sample data for testing.
"""
import asyncio
import os
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient


async def setup_database():
    """Setup MongoDB collections, indexes, and sample data"""

    # Use environment variable or default to localhost
    mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    mongo_db = os.getenv("MONGODB_DB", "interviews")
    
    client = AsyncIOMotorClient(mongo_uri)
    db = client[mongo_db]

    print("Setting up MongoDB database...")

    # Check if data already exists
    existing_interview = await db.interviews.find_one({})
    if existing_interview:
        print("\n[WARNING] Database already has data!")
        print("Run 'uv run python clear_db.py' first to clear it.")
        print("Aborting setup to prevent duplicates.\n")
        client.close()
        return

    # Create indexes
    print("\nCreating indexes...")

    # Transcripts indexes
    await db.transcripts.create_index([("interview_id", 1), ("timestamp", -1)])
    await db.transcripts.create_index("speaker")
    print("   [OK] Transcripts indexes created")

    # Interviews indexes
    await db.interviews.create_index("candidate_id")
    await db.interviews.create_index("job_id")
    await db.interviews.create_index("status")
    await db.interviews.create_index([("started_at", -1)])
    print("   [OK] Interviews indexes created")

    # Candidates indexes
    await db.candidates.create_index("email", unique=True)
    print("   [OK] Candidates indexes created")

    # Jobs indexes
    await db.jobs.create_index("company_id")
    await db.jobs.create_index([("title", "text")])
    print("   [OK] Jobs indexes created")

    # Insert sample data
    print("\nCreating sample data...")

    # Sample company
    company_result = await db.companies.insert_one({
        "name": "TechCorp Inc.",
        "info": open("tests/fixtures/sample_company.txt", encoding="utf-8").read(),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    })
    company_id = company_result.inserted_id
    print(f"   [OK] Company created: {company_id}")

    # Sample job
    job_result = await db.jobs.insert_one({
        "title": "Senior Backend Engineer",
        "description": open("tests/fixtures/sample_jd.txt", encoding="utf-8").read(),
        "company_id": company_id,
        "required_skills": ["Python", "MongoDB", "REST APIs", "Docker", "Redis"],
        "experience_level": "Senior",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    })
    job_id = job_result.inserted_id
    print(f"   [OK] Job created: {job_id}")

    # Sample candidate
    candidate_result = await db.candidates.insert_one({
        "name": "John Doe",
        "email": "john.doe@example.com",
        "cv_text": open("tests/fixtures/sample_cv.txt", encoding="utf-8").read(),
        "phone": "+1-555-0123",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    })
    candidate_id = candidate_result.inserted_id
    print(f"   [OK] Candidate created: {candidate_id}")

    # Sample interview
    interview_result = await db.interviews.insert_one({
        "candidate_id": candidate_id,
        "job_id": job_id,
        "status": "in_progress",
        "phase": "INTRO",
        "turn_count": 0,
        "phase_turn_count": 0,  # Turns in current phase
        "conversation_summary": "",
        "started_at": datetime.utcnow(),
        "ended_at": None,
        "limits": {
            "max_duration_hit": False,
            "context_overflow": False,
            "forced_end_reason": None
        },
        "evaluation": None,  # Will be populated when interview completes
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    })
    interview_id = interview_result.inserted_id
    print(f"   [OK] Interview created: {interview_id}")

    # IMPORTANT: Agent sends the FIRST message (greeting)
    await db.transcripts.insert_one({
        "interview_id": interview_id,
        "speaker": "agent",
        "text": "Hello! Welcome to your interview for the Senior Backend Engineer position at TechCorp Inc. I'm excited to learn more about your background and experience. How are you doing today?",
        "timestamp": datetime.utcnow(),
        "audio_url": None,
        "metadata": {
            "phase": "INTRO",
            "turn_count": 0,
            "is_fallback": False,
            "llm_provider": "system",
            "estimated_tokens": 0,
            "forced_end": False,
            "reason": "initial_greeting"
        }
    })
    print(f"   [OK] Initial agent greeting sent")

    print("\n" + "="*60)
    print("[DONE] Database setup complete!")
    print("="*60)
    print(f"\nSample Interview ID: {interview_id}")
    print(f"Run 'python run_demo.py' to start the agent and send test messages")

    client.close()


if __name__ == "__main__":
    asyncio.run(setup_database())
