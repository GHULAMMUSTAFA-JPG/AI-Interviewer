"""
STT Speech Buffer — buffers candidate speech and saves to MongoDB.

Handles:
- Accumulating speech segments
- Noise filtering (min 2 words)
- Deduplication
- Delayed flush (500ms after SPEECH_END, 2s safety net)
- MongoDB persistence
"""
import asyncio
from logger import push_log
from mongo_handler import insert_transcript

# Global speech state
_speech_buffer = ""
_speech_timer = None
_last_saved_text = ""
last_speech_time = 0  # Initialized when event loop starts

# Single-word responses that are valid interview answers — never noise-filter these
VALID_SHORT_ANSWERS = {
    "yes", "no", "yeah", "nope", "yep", "nah", "right", "correct", "wrong",
    "okay", "ok", "sure", "fine", "agreed", "agree", "exactly", "absolutely",
    "definitely", "certainly", "perhaps", "maybe", "possibly", "true", "false",
    "never", "always", "sometimes", "often", "rarely", "unclear", "unsure"
}


def init_speech_state():
    """Initialize global speech state (call once when event loop starts)."""
    global last_speech_time
    last_speech_time = asyncio.get_event_loop().time()


async def _delayed_flush(interview_id: str, delay_sec: float) -> None:
    """Wait delay_sec seconds, then flush the speech buffer if it has content."""
    await asyncio.sleep(delay_sec)
    if get_speech_buffer().strip():
        await _flush_speech_buffer(interview_id)


async def _flush_speech_buffer(interview_id: str) -> None:
    """Save buffered speech to DB after candidate stops speaking."""
    global _speech_buffer, _speech_timer, _last_saved_text, last_speech_time

    # Clear the timer reference
    _speech_timer = None

    if _speech_buffer.strip():
        current_text = _speech_buffer.strip()

        # Filter noise artifacts — require at least 2 words unless it's a known valid short answer
        words = current_text.split()
        is_valid_short = (
            len(words) == 1
            and current_text.lower().rstrip(".,!?") in VALID_SHORT_ANSWERS
        )
        if len(words) < 2 and not is_valid_short:
            msg = f"[NOISE FILTERED] Too short ({len(words)} word): \"{current_text}\""
            await push_log(msg)
            _speech_buffer = ""
            return

        # Dedup: don't save identical text twice in a row
        if current_text == _last_saved_text:
            msg = "[DUPLICATE PREVENTED] Same text as last save -- discarding"
            await push_log(msg)
            _speech_buffer = ""
            return

        # Save to DB
        await insert_transcript(interview_id, "candidate", current_text)
        msg = f"[SAVED TO DB] \"{current_text[:120]}{'...' if len(current_text) > 120 else ''}\""
        await push_log(msg)

        _last_saved_text = current_text
        last_speech_time = asyncio.get_event_loop().time()
        _speech_buffer = ""


# ─── Buffer Accessors ───────────────────────────────────────────────

def get_speech_buffer():
    """Get current speech buffer content."""
    return _speech_buffer


def set_speech_buffer(text: str):
    """Set speech buffer content."""
    global _speech_buffer
    _speech_buffer = text


def clear_speech_buffer():
    """Clear speech buffer without saving. Preserves _last_saved_text for dedup."""
    global _speech_buffer, _speech_timer
    _speech_buffer = ""
    _speech_timer = None


def reset_session():
    """Full reset for a new interview session — clears buffer AND dedup state."""
    global _speech_buffer, _speech_timer, _last_saved_text
    _speech_buffer = ""
    _speech_timer = None
    _last_saved_text = ""


def get_last_saved_text():
    """Get last saved text for dedup."""
    return _last_saved_text


def get_speech_timer():
    """Get current speech timer."""
    return _speech_timer


def set_speech_timer(timer):
    """Set speech timer reference."""
    global _speech_timer
    _speech_timer = timer


def cancel_pending_flush():
    """Cancel any pending delayed flush task. Call this on interview shutdown."""
    global _speech_timer
    if _speech_timer and not _speech_timer.done():
        _speech_timer.cancel()
    _speech_timer = None


def update_last_speech_time():
    """Update last speech timestamp."""
    global last_speech_time
    last_speech_time = asyncio.get_event_loop().time()


def get_last_speech_time():
    """Get last speech timestamp."""
    return last_speech_time
