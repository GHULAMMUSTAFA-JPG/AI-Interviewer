"""
Speech Buffer Management — handles candidate speech buffering and DB persistence.
"""
import asyncio
from logger import push_log
from mongo_handler import insert_transcript

# Global speech state
_speech_buffer = ""
_speech_timer = None
_last_saved_text = ""
last_speech_time = 0  # Will be initialized when event loop starts


def init_speech_state():
    """Initialize global speech state (call once when event loop starts)."""
    global last_speech_time
    last_speech_time = asyncio.get_event_loop().time()


async def _flush_speech_buffer(interview_id: str) -> None:
    """Save buffered speech to DB after candidate stops speaking."""
    global _speech_buffer, _speech_timer, _last_saved_text, last_speech_time

    # Clear the timer reference without cancelling — this function IS the task
    _speech_timer = None

    if _speech_buffer.strip():
        current_text = _speech_buffer.strip()

        # Dedup: don't save identical text twice in a row
        if current_text == _last_saved_text:
            msg = "⚠️  [DUPLICATE PREVENTED] Same text as last save — discarding"
            print(msg); await push_log(msg)
            _speech_buffer = ""
            return

        # Save to DB
        await insert_transcript(interview_id, "candidate", current_text)
        msg = f"💬 [SAVED TO DB] \"{current_text[:120]}{'...' if len(current_text) > 120 else ''}\""
        print(msg); await push_log(msg)

        _last_saved_text = current_text
        last_speech_time = asyncio.get_event_loop().time()  # reset inactivity clock
        _speech_buffer = ""


def get_speech_buffer():
    """Get current speech buffer content."""
    return _speech_buffer


def set_speech_buffer(text: str):
    """Set speech buffer content."""
    global _speech_buffer
    _speech_buffer = text


def clear_speech_buffer():
    """Clear speech buffer without saving."""
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


def update_last_speech_time():
    """Update last speech timestamp."""
    global last_speech_time
    last_speech_time = asyncio.get_event_loop().time()


def get_last_speech_time():
    """Get last speech timestamp."""
    return last_speech_time
