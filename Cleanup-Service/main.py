"""
Interview Cleanup Service - Watchdog Supervisor
─────────────────────────────────────────────────────────────────────
Runs every 30 seconds to detect and recover stuck interviews.

Detects stuck interviews via:
- Bot heartbeat timeout (> 2 minutes since last heartbeat)
- Interview duration timeout (> 2 hours total)
- No heartbeat ever sent (bot crashed before first heartbeat)

This is a reconciliation controller - independent of bot/agent failures.
"""

import asyncio
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = "mongodb://mongodb:27017/?replicaSet=rs0"
DB_NAME = "interviews"

# Timeouts
HEARTBEAT_TIMEOUT_MINUTES = 2
MAX_INTERVIEW_DURATION_MINUTES = 120
CLEANUP_INTERVAL_SECONDS = 30  # Run every 30 seconds for fast detection

# Global client - reused across runs (connection pooling)
_client = None
_db = None


async def get_db():
    """Get or create MongoDB client (singleton pattern)."""
    global _client, _db
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        # Force connection test
        await _client.admin.command("ping")
        _db = _client[DB_NAME]
    return _db


async def cleanup_stuck_interviews():
    """Find and mark stuck interviews as abandoned."""
    db = await get_db()
    
    try:
        cutoff_heartbeat = datetime.utcnow() - timedelta(minutes=HEARTBEAT_TIMEOUT_MINUTES)
        cutoff_duration = datetime.utcnow() - timedelta(minutes=MAX_INTERVIEW_DURATION_MINUTES)
        
        # Find stuck interviews
        stuck_query = {
            "status": "in_progress",
            "$or": [
                # Bot heartbeat is stale
                {"bot_heartbeat": {"$lt": cutoff_heartbeat}},
                # No heartbeat ever (bot crashed before first heartbeat)
                {
                    "$and": [
                        {"bot_heartbeat": {"$exists": False}},
                        {"created_at": {"$lt": cutoff_duration}}
                    ]
                },
                # Interview has been running too long
                {"created_at": {"$lt": cutoff_duration}}
            ]
        }
        
        stuck_interviews = await db.interviews.find(stuck_query).to_list(length=100)
        
        if not stuck_interviews:
            print("🧹 Cleanup: No stuck interviews found")
            return 0
        
        cleaned_count = 0
        for interview in stuck_interviews:
            interview_id = interview.get("interview_id", "unknown")
            created_at = interview.get("created_at", datetime.utcnow())
            duration_minutes = (datetime.utcnow() - created_at).total_seconds() / 60
            
            # Determine why it's stuck
            bot_heartbeat = interview.get("bot_heartbeat")
            if bot_heartbeat:
                time_since_heartbeat = (datetime.utcnow() - bot_heartbeat).total_seconds() / 60
                reason = f"bot_heartbeat_stale ({time_since_heartbeat:.1f}min ago)"
            elif not interview.get("bot_heartbeat") and created_at < cutoff_duration:
                reason = "no_heartbeat_interview_too_old"
            else:
                reason = "interview_duration_exceeded"
            
            # Mark as abandoned (atomic update - safe for multi-replica)
            result = await db.interviews.update_one(
                {"_id": interview["_id"], "status": "in_progress"},  # ← Soft locking
                {"$set": {
                    "status": "abandoned",
                    "ended_at": datetime.utcnow(),
                    "abandon_reason": reason,
                    "abandon_duration_minutes": round(duration_minutes, 1)
                }}
            )
            
            if result.modified_count > 0:
                print(f"🧹 Cleanup: Marked interview {interview_id[:8]} as abandoned ({reason})")
                cleaned_count += 1
        
        print(f"🧹 Cleanup complete: {cleaned_count}/{len(stuck_interviews)} interviews cleaned")
        
        return cleaned_count
        
    except Exception as e:
        print(f"❌ Cleanup error: {e}")
        return 0


async def main():
    """Run cleanup service continuously."""
    print("🧹 Starting Interview Cleanup Service")
    print(f"   Heartbeat timeout: {HEARTBEAT_TIMEOUT_MINUTES} minutes")
    print(f"   Max interview duration: {MAX_INTERVIEW_DURATION_MINUTES} minutes")
    print(f"   Cleanup interval: {CLEANUP_INTERVAL_SECONDS} seconds")
    
    # Initialize DB connection
    try:
        await get_db()
        print("✅ Connected to MongoDB")
    except Exception as e:
        print(f"❌ Failed to connect to MongoDB: {e}")
        return
    
    while True:
        try:
            await cleanup_stuck_interviews()
        except Exception as e:
            print(f"❌ Cleanup loop error: {e}")
        
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
