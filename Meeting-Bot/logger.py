import time as _time


async def push_log(message: str, interview_id: str = None) -> None:
    """Print a timestamped log line. Prefix with short interview_id when provided."""
    ts = _time.strftime("%H:%M:%S")
    tag = f"[{interview_id[:8]}] " if interview_id else ""
    print(f"[{ts}] {tag}{message}")
