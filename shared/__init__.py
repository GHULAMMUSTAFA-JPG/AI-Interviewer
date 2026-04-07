# Shared package — common utilities used across all services
from shared.redis_client import get_redis, RedisState, close_redis
from shared.logger import push_log
from shared.exceptions import DatabaseException, DocumentNotFoundError

__all__ = [
    "get_redis",
    "RedisState", 
    "close_redis",
    "push_log",
    "DatabaseException",
    "DocumentNotFoundError",
]
