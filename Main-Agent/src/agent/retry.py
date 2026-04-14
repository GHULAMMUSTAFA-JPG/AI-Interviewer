"""
Retry logic with exponential backoff for LLM calls.
"""
import asyncio
from functools import wraps
from src.config import logger


def retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0):
    """
    Decorator for retry with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds (doubles each retry)

    Usage:
        @retry_with_backoff(max_retries=3, base_delay=1.0)
        async def my_function():
            ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)

                except Exception as e:
                    last_exception = e

                    # Don't retry on last attempt
                    if attempt >= max_retries:
                        break

                    # Calculate delay with exponential backoff
                    delay = base_delay * (2 ** attempt)

                    logger.warning(
                        f"Retry {attempt + 1}/{max_retries} for {func.__name__} "
                        f"after {delay}s delay. Error: {str(e)[:100]}"
                    )

                    await asyncio.sleep(delay)

            # All retries exhausted
            logger.error(f"All {max_retries} retries failed for {func.__name__}")
            raise last_exception

        return wrapper
    return decorator


class CircuitBreaker:
    """
    Circuit breaker pattern for LLM calls.

    States:
    - CLOSED: Normal operation
    - OPEN: Too many failures, reject calls immediately
    - HALF_OPEN: Testing if service recovered
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        """
        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before trying again
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    async def call(self, func, *args, **kwargs):
        """Execute function through circuit breaker"""

        # Check if circuit is open
        if self.state == "OPEN":
            # Check if recovery timeout passed
            if self.last_failure_time:
                time_since_failure = (asyncio.get_running_loop().time() - self.last_failure_time)
                if time_since_failure >= self.recovery_timeout:
                    logger.info("[CIRCUIT BREAKER] Entering HALF_OPEN state")
                    self.state = "HALF_OPEN"
                else:
                    raise Exception(f"Circuit breaker OPEN (failed {self.failure_count} times)")

        try:
            result = await func(*args, **kwargs)

            # Success - reset if in HALF_OPEN
            if self.state == "HALF_OPEN":
                logger.info("[CIRCUIT BREAKER] Service recovered - returning to CLOSED")
                self.state = "CLOSED"
                self.failure_count = 0

            return result

        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = asyncio.get_running_loop().time()

            logger.warning(
                f"[CIRCUIT BREAKER] Failure {self.failure_count}/{self.failure_threshold}: {str(e)[:100]}"
            )

            # Open circuit if threshold exceeded
            if self.failure_count >= self.failure_threshold:
                logger.error(
                    f"[CIRCUIT BREAKER] OPENING circuit after {self.failure_count} failures"
                )
                self.state = "OPEN"
                # Write to Redis so UI and operators can detect degraded state
                try:
                    import asyncio as _asyncio
                    _asyncio.ensure_future(self._write_redis_status("circuit_open"))
                except Exception:
                    pass

            raise

    async def _write_redis_status(self, status: str) -> None:
        """Write circuit breaker state to Redis for operator visibility."""
        try:
            from src.redis_client import get_redis
            redis = await get_redis()
            await redis.set(f"circuit_breaker:{id(self)}:status", status, ex=300)
        except Exception:
            pass  # Never block on Redis unavailability


# Global circuit breaker instance for main interview LLM calls
llm_circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60
)

# Separate circuit breaker for summary generation.
# Keeps summary failures isolated so they cannot open the main circuit.
summary_circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=60
)
