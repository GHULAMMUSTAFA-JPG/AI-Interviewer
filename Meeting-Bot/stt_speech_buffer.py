"""
STT Speech Buffer — per-interview state via Redis.

Each interview has its own isolated speech buffer, timer, and dedup state
stored in Redis. This allows concurrent interviews without cross-contamination.

Keys per interview:
  speech:{interview_id}:buffer     — accumulated text
  speech:{interview_id}:last_saved — last saved text (for dedup)
  speech:{interview_id}:timer      — asyncio.Task reference (in-memory only)
"""
import asyncio
from logger import push_log
from mongo_handler import insert_transcript

# In-memory timer references (per-interview)
_speech_timers: dict[str, asyncio.Task] = {}

# Short-word responses that are valid interview answers
VALID_SHORT_ANSWERS = {
    "yes", "no", "yeah", "nope", "yep", "nah", "right", "correct", "wrong",
    "okay", "ok", "sure", "fine", "agreed", "agree", "exactly", "absolutely",
    "definitely", "certainly", "perhaps", "maybe", "possibly", "true", "false",
    "never", "always", "sometimes", "often", "rarely", "unclear", "unsure"
}

_last_speech_time: dict[str, float] = {}  # interview_id → timestamp


def init_speech_state():
    """Initialize global speech state (call once when event loop starts)."""
    pass  # No global state to init anymore


async def _get_buffer(redis, interview_id: str) -> str:
    """Get speech buffer for this interview."""
    val = await redis.get(f"speech:{interview_id}:buffer")
    return val.decode() if val else ""


async def _set_buffer(redis, interview_id: str, text: str):
    """Set speech buffer for this interview."""
    await redis.set(f"speech:{interview_id}:buffer", text)


async def _get_last_saved(redis, interview_id: str) -> str:
    """Get last saved text for dedup."""
    val = await redis.get(f"speech:{interview_id}:last_saved")
    return val.decode() if val else ""


async def _set_last_saved(redis, interview_id: str, text: str):
    """Set last saved text for dedup."""
    await redis.set(f"speech:{interview_id}:last_saved", text)


async def _delayed_flush(redis, interview_id: str, delay_sec: float) -> None:
    """Wait delay_sec seconds, then flush the speech buffer if it has content."""
    await asyncio.sleep(delay_sec)
    _speech_timers.pop(interview_id, None)
    buf = await _get_buffer(redis, interview_id)
    if buf.strip():
        await _flush_speech_buffer(redis, interview_id)


async def _flush_speech_buffer(redis, interview_id: str) -> None:
    """Save buffered speech to DB after candidate stops speaking."""
    _speech_timers.pop(interview_id, None)

    current_text = (await _get_buffer(redis, interview_id)).strip()

    if not current_text:
        return

    # Filter noise artifacts — require at least 2 words unless it's a known valid short answer
    words = current_text.split()
    is_valid_short = (
        len(words) == 1
        and current_text.lower().rstrip(".,!?") in VALID_SHORT_ANSWERS
    )
    if len(words) < 2 and not is_valid_short:
        msg = f"[NOISE FILTERED] Too short ({len(words)} word): \"{current_text}\""
        await push_log(msg)
        await _set_buffer(redis, interview_id, "")
        return

    # Dedup: don't save identical text twice in a row
    last_saved = await _get_last_saved(redis, interview_id)
    if current_text == last_saved:
        msg = "[DUPLICATE PREVENTED] Same text as last save -- discarding"
        await push_log(msg)
        await _set_buffer(redis, interview_id, "")
        return

    # Save to DB
    await insert_transcript(interview_id, "candidate", current_text)
    msg = f"[SAVED TO DB] \"{current_text[:120]}{'...' if len(current_text) > 120 else ''}\""
    await push_log(msg)

    await _set_last_saved(redis, interview_id, current_text)
    await _set_buffer(redis, interview_id, "")
    _last_speech_time[interview_id] = asyncio.get_event_loop().time()


# ─── Buffer Accessors (Redis-backed) ───────────────────────────────

async def append_speech(redis, interview_id: str, text: str):
    """Append text to this interview's speech buffer."""
    key = f"speech:{interview_id}:buffer"
    await redis.append(key, text + " ")


async def clear_speech_buffer(redis, interview_id: str):
    """Clear speech buffer for this interview. Preserves dedup state."""
    await _set_buffer(redis, interview_id, "")
    _speech_timers.pop(interview_id, None)


async def reset_session(redis, interview_id: str):
    """Full reset for a new interview session."""
    await _set_buffer(redis, interview_id, "")
    await _set_last_saved(redis, interview_id, "")
    _speech_timers.pop(interview_id, None)
    _last_speech_time.pop(interview_id, None)


def get_last_speech_time(interview_id: str = None) -> float:
    """Get last speech timestamp for an interview (or global fallback)."""
    if interview_id:
        return _last_speech_time.get(interview_id, 0)
    return max(_last_speech_time.values()) if _last_speech_time else 0


def update_last_speech_time(interview_id: str):
    """Update last speech timestamp for an interview."""
    _last_speech_time[interview_id] = asyncio.get_event_loop().time()


def cancel_pending_flush(interview_id: str):
    """Cancel pending flush for an interview. Call on shutdown."""
    task = _speech_timers.pop(interview_id, None)
    if task and not task.done():
        task.cancel()
