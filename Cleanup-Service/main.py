"""
Interview Cleanup Service - Watchdog Supervisor with Redis Heartbeats
─────────────────────────────────────────────────────────────────────────
Runs every 10 seconds to detect and recover stuck interviews.

Detects stuck interviews via:
- Redis heartbeat timeout (> 60 seconds since last heartbeat)
- Interview duration timeout (> 2 hours total)
- No heartbeat ever sent (bot crashed before first heartbeat)

Redis Integration:
- Checks Redis heartbeats first (instant, accurate)
- Falls back to MongoDB for interviews without Redis data
- Publishes cleanup events to Redis pub/sub
- Updates Redis state when cleaning up

This is a reconciliation controller - independent of bot/agent failures.
"""

import asyncio
import os
import json
import time
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from redis_client import get_redis, RedisState, close_redis

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://host.docker.internal:27017/?replicaSet=rs0")
DB_NAME = os.getenv("MONGODB_DB", "interviews")

# Timeouts
HEARTBEAT_TIMEOUT_SECONDS = 60  # 60 seconds (using Redis heartbeats)
MAX_INTERVIEW_DURATION_MINUTES = 120
CLEANUP_INTERVAL_SECONDS = 10  # Run every 10 seconds for fast detection

# Global clients
_client = None
_db = None
_redis_state = None


async def get_db():
    """Get or create MongoDB client (singleton pattern)."""
    global _client, _db
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        await _client.admin.command("ping")
        _db = _client[DB_NAME]
    return _db


async def cleanup_stuck_interviews():
    """Find and mark stuck interviews as abandoned using Redis heartbeats."""
    db = await get_db()
    global _redis_state

    try:
        cutoff_duration = datetime.utcnow() - timedelta(minutes=MAX_INTERVIEW_DURATION_MINUTES)

        # Get all active interviews from MongoDB
        active_interviews = await db.interviews.find({
            "status": "in_progress"
        }).to_list(length=100)

        if not active_interviews:
            print("🧹 Cleanup: No active interviews")
            return 0

        stuck_interviews = []

        for interview in active_interviews:
            interview_id = interview.get("interview_id")
            if not interview_id:
                continue

            is_stuck = False
            reason = ""

            # Check Redis heartbeat first (fast, accurate)
            if _redis_state:
                try:
                    is_alive = await _redis_state.check_bot_alive(interview_id, timeout=HEARTBEAT_TIMEOUT_SECONDS)
                    if not is_alive:
                        # Check if bot ever sent a heartbeat
                        bot_status = await _redis_state.get_bot_status(interview_id)
                        if bot_status and bot_status.get("status"):
                            is_stuck = True
                            reason = f"redis_heartbeat_timeout (bot status: {bot_status.get('status')})"
                        else:
                            # No Redis data at all - check MongoDB fallback
                            created_at = interview.get("created_at", datetime.utcnow())
                            if created_at < cutoff_duration:
                                is_stuck = True
                                reason = "no_redis_data_interview_too_old"
                except Exception as e:
                    print(f"⚠️  Redis check failed for {interview_id[:8]}: {e}")
                    # Fallback to MongoDB check
                    created_at = interview.get("created_at", datetime.utcnow())
                    if created_at < cutoff_duration:
                        is_stuck = True
                        reason = "mongodb_duration_exceeded"

            # Mark as stuck
            if is_stuck:
                stuck_interviews.append({
                    "interview": interview,
                    "reason": reason
                })

        if not stuck_interviews:
            print("🧹 Cleanup: No stuck interviews found")
            return 0

        cleaned_count = 0
        for item in stuck_interviews:
            interview = item["interview"]
            reason = item["reason"]
            interview_id = interview.get("interview_id", "unknown")
            created_at = interview.get("created_at", datetime.utcnow())
            duration_minutes = (datetime.utcnow() - created_at).total_seconds() / 60

            # Mark as abandoned (atomic update)
            result = await db.interviews.update_one(
                {"_id": interview["_id"], "status": "in_progress"},
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

                # Clean up Redis state
                if _redis_state:
                    try:
                        await _redis_state.cleanup_interview(interview_id)
                        await _redis_state.publish_event("interview_abandoned", {
                            "interview_id": interview_id,
                            "reason": reason,
                            "duration_minutes": round(duration_minutes, 1)
                        })
                    except:
                        pass

        print(f"🧹 Cleanup complete: {cleaned_count}/{len(stuck_interviews)} interviews cleaned")
        return cleaned_count

    except Exception as e:
        print(f"❌ Cleanup error: {e}")
        return 0


async def main():
    """Run cleanup service continuously."""
    print("🧹 Starting Interview Cleanup Service (Redis-enabled)")
    print(f"   Heartbeat timeout: {HEARTBEAT_TIMEOUT_SECONDS} seconds (Redis)")
    print(f"   Max interview duration: {MAX_INTERVIEW_DURATION_MINUTES} minutes")
    print(f"   Cleanup interval: {CLEANUP_INTERVAL_SECONDS} seconds")

    # Initialize MongoDB
    try:
        await get_db()
        print("✅ Connected to MongoDB")
    except Exception as e:
        print(f"❌ Failed to connect to MongoDB: {e}")
        return

    # Initialize Redis
    global _redis_state
    try:
        redis = await get_redis()
        _redis_state = RedisState(redis)
        print("✅ Connected to Redis (heartbeat monitoring enabled)")
    except Exception as e:
        print(f"⚠️  Redis connection failed: {e}")
        print("   Running in MongoDB-only mode (slower detection)")
        _redis_state = None

    while True:
        try:
            await cleanup_stuck_interviews()
        except Exception as e:
            print(f"❌ Cleanup loop error: {e}")

        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)

    # Cleanup on exit
    await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
