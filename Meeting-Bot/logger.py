import time as _time


async def push_log(message: str) -> None:
    """Async log — prints once to stdout with timestamp."""
    ts = _time.strftime("%H:%M:%S")
    print(f"[{ts}] {message}")
