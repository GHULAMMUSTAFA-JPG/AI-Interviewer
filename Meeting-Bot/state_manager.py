"""
Bot State Manager — writes bot status to MongoDB and Redis.
"""
from datetime import datetime
from logger import push_log


async def set_bot_state(interview_id: str, bot_status: str, db=None, mongo_connected: bool = False) -> None:
    """Write bot_status to both MongoDB and Redis at every state transition."""
    # MongoDB update
    if mongo_connected and db is not None:
        try:
            await db.interviews.update_one(
                {"interview_id": interview_id},
                {"$set": {"bot_status": bot_status, "bot_status_updated_at": datetime.utcnow()}}
            )
        except Exception as e:
            print(f"⚠️  set_bot_state MongoDB error ({bot_status}): {e}")

    # Redis update
    try:
        from redis_client import get_redis, RedisState
        redis = await get_redis()
        state = RedisState(redis)
        await state.set_bot_status(interview_id, bot_status)
        # Mirror bot_status → meeting status so UI shows meaningful labels
        _meeting_status_map = {
            "joining":   "preparing",
            "waiting":   "waiting",    # bot in lobby
            "admitted":  "in_meeting", # bot inside the call
            "completed": "completed",
            "abandoned": "abandoned",
        }
        if bot_status in _meeting_status_map:
            await state.set_meeting_status(interview_id, _meeting_status_map[bot_status])
    except Exception as e:
        print(f"⚠️  set_bot_state Redis error ({bot_status}): {e}")
