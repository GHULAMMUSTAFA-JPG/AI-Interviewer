"""
Shared Logger for AI Interviewer Services

Provides a unified logging interface across all services.
Logs are pushed to stdout for Docker log collection.
"""


async def push_log(message: str) -> None:
    """Push a log message to stdout (captured by Docker logs)."""
    print(message)
