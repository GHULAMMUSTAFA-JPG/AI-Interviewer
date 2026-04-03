"""
Redis Connection Utility for AI Interviewer Services

Provides a shared Redis connection pool and helper functions
for state management, pub/sub, and caching.

Usage:
    from redis_client import get_redis, RedisState
    
    # Get Redis connection
    redis = await get_redis()
    
    # Use state management
    state = RedisState(redis)
    await state.set_bot_status(interview_id, "admitted")
"""

import os
import json
import time
import asyncio
from typing import Optional, Dict, Any
from redis.asyncio import Redis, ConnectionPool

# Global connection pool
_redis_pool: Optional[ConnectionPool] = None
_redis_client: Optional[Redis] = None


async def get_redis() -> Redis:
    """Get or create Redis connection with connection pooling."""
    global _redis_pool, _redis_client
    
    if _redis_client is None:
        redis_uri = os.getenv("REDIS_URI", "redis://localhost:6379/0")
        
        _redis_pool = ConnectionPool.from_url(
            redis_uri,
            max_connections=20,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True
        )
        
        _redis_client = Redis(connection_pool=_redis_pool)
        
        # Test connection
        try:
            await _redis_client.ping()
            print(f"✅ Connected to Redis: {redis_uri}")
        except Exception as e:
            print(f"❌ Redis connection failed: {e}")
            raise
    
    return _redis_client


async def close_redis():
    """Close Redis connection pool."""
    global _redis_pool, _redis_client
    
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
    
    if _redis_pool:
        await _redis_pool.disconnect()
        _redis_pool = None


class RedisState:
    """Helper class for managing bot and meeting state in Redis."""
    
    def __init__(self, redis: Redis):
        self.redis = redis
    
    # ─────────────────────────────────────────────────────────────────────
    # Bot State Management
    # ─────────────────────────────────────────────────────────────────────
    
    async def set_bot_status(self, interview_id: str, status: str, **kwargs):
        """Update bot status in Redis."""
        key = f"bot:{interview_id}:status"
        data = {
            "status": status,
            "updated_at": str(time.time()),
            **kwargs
        }
        await self.redis.hset(key, mapping=data)
        
        # Publish event
        await self.publish_event("bot_status_changed", {
            "interview_id": interview_id,
            "status": status,
            **kwargs
        })
    
    async def get_bot_status(self, interview_id: str) -> Dict[str, str]:
        """Get current bot status from Redis."""
        key = f"bot:{interview_id}:status"
        return await self.redis.hgetall(key)
    
    async def set_bot_heartbeat(self, interview_id: str):
        """Update bot heartbeat (TTL: 60s)."""
        key = f"bot:{interview_id}:heartbeat"
        await self.redis.setex(key, 60, str(time.time()))
    
    async def check_bot_alive(self, interview_id: str, timeout: int = 60) -> bool:
        """Check if bot is alive based on heartbeat."""
        key = f"bot:{interview_id}:heartbeat"
        last_hb = await self.redis.get(key)
        
        if not last_hb:
            return False
        
        return (time.time() - float(last_hb)) < timeout
    
    # ─────────────────────────────────────────────────────────────────────
    # Meeting State Management
    # ─────────────────────────────────────────────────────────────────────
    
    async def set_meeting_status(self, interview_id: str, status: str, **kwargs):
        """Update meeting status in Redis."""
        key = f"meeting:{interview_id}:status"
        data = {
            "status": status,
            "updated_at": str(time.time()),
            **kwargs
        }
        await self.redis.hset(key, mapping=data)
        
        # Publish event
        await self.publish_event("meeting_status_changed", {
            "interview_id": interview_id,
            "status": status,
            **kwargs
        })
    
    async def get_meeting_status(self, interview_id: str) -> Dict[str, str]:
        """Get current meeting status from Redis."""
        key = f"meeting:{interview_id}:status"
        return await self.redis.hgetall(key)
    
    # ─────────────────────────────────────────────────────────────────────
    # Pub/Sub Events
    # ─────────────────────────────────────────────────────────────────────
    
    async def publish_event(self, event: str, data: Dict[str, Any]):
        """Publish event to Redis pub/sub channel."""
        message = json.dumps({
            "event": event,
            "data": data,
            "timestamp": time.time()
        })
        await self.redis.publish("interview.events", message)
    
    # ─────────────────────────────────────────────────────────────────────
    # Caching
    # ─────────────────────────────────────────────────────────────────────
    
    async def cache_set(self, key: str, value: Any, ttl: int = 300):
        """Set cache with TTL (default: 5 minutes)."""
        await self.redis.setex(
            f"cache:{key}",
            ttl,
            json.dumps(value) if not isinstance(value, str) else value
        )
    
    async def cache_get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        value = await self.redis.get(f"cache:{key}")
        if value:
            try:
                return json.loads(value)
            except:
                return value
        return None
    
    async def cache_delete(self, key: str):
        """Delete cache entry."""
        await self.redis.delete(f"cache:{key}")
    
    # ─────────────────────────────────────────────────────────────────────
    # Cleanup
    # ─────────────────────────────────────────────────────────────────────
    
    async def cleanup_interview(self, interview_id: str):
        """Remove all Redis keys for an interview."""
        keys = await self.redis.keys(f"*{interview_id}*")
        if keys:
            await self.redis.delete(*keys)
