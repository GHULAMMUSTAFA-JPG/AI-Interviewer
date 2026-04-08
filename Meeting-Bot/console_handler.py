"""
Console Handler — captures and processes Web Speech API events from browser console.
Handles STT event parsing, echo cancellation, and interrupt triggering.

FIX: Don't flush every TRANSCRIPT_EVENT immediately. The JavaScript silence timer
(1500ms) handles batching. Flushing immediately in Python defeats the timer and
causes fragmented saves. Instead, accumulate and flush on SPEECH_END or after a
delayed timer (2s safety net).
"""
import asyncio
from logger import push_log
from speech_buffer import (
    get_speech_buffer, set_speech_buffer, clear_speech_buffer, get_speech_timer,
    set_speech_timer, update_last_speech_time, get_last_speech_time
)


async def _delayed_flush(interview_id: str, delay_sec: float) -> None:
    """Wait delay_sec seconds, then flush the speech buffer if it has content."""
    await asyncio.sleep(delay_sec)
    if get_speech_buffer().strip():
        from speech_buffer import _flush_speech_buffer
        await _flush_speech_buffer(interview_id)



async def create_console_handler(page, interview_id: str, status_state: dict,
                                  db=None, mongo_connected: bool = False,
                                  flush_callback=None):
    """
    Create and attach console event handler to capture Web Speech API transcripts.
    
    Args:
        page: Playwright page object
        interview_id: Current interview ID
        status_state: Dict with {"bot_speaking": bool}
        db: MongoDB database instance
        mongo_connected: Whether MongoDB is connected
        flush_callback: Async function to call when transcript is ready
    
    Returns:
        Handler function (for cleanup if needed)
    """

    async def _handle_console(msg):
        try:
            text = msg.text

            # Interim results — real-time partial transcript, log only.
            if text.startswith('STT_INTERIM:'):
                interim_text = text.split('STT_INTERIM:', 1)[1].strip()
                if interim_text:
                    msg_log = f"🎤 [INTERIM] {interim_text[:120]}"
                    print(msg_log)  # stdout only, not push_log (too noisy)
                return

            # Capture speech start — update inactivity clock always.
            # Only clear the buffer on interrupt (bot is speaking), NOT on normal speech.
            # Clearing on every utterance start would drop partial text from a previous
            # segment if the user pauses briefly and starts speaking again.
            if text.startswith('STT_SPEECH_START:'):
                update_last_speech_time()
                if status_state["bot_speaking"]:
                    # Candidate is cutting in — clear stale buffer and signal interrupt
                    timer = get_speech_timer()
                    if timer and not timer.done():
                        timer.cancel()
                        set_speech_timer(None)
                    clear_speech_buffer()
                    if mongo_connected and db is not None:
                        try:
                            await db.interviews.update_one(
                                {"interview_id": interview_id},
                                {"$set": {"tts_interrupt": True}}
                            )
                            msg_log = "⚡ [INTERRUPT] Candidate interrupted bot"
                            print(msg_log); await push_log(msg_log)
                        except Exception as int_err:
                            print(f"⚠️ Interrupt signal error: {int_err}")
                else:
                    msg_log = "🎤 [SPEECH STARTED] Candidate speaking..."
                    print(msg_log); await push_log(msg_log)
                return

            # Speech ended — flush the accumulated buffer after a short delay
            # to catch any final TRANSCRIPT_EVENT that may follow.
            if text.startswith('STT_SPEECH_END:'):
                msg_log = "🔇 [SPEECH ENDED] Waiting for final transcript..."
                print(msg_log); await push_log(msg_log)

                # Flush after 500ms to catch any last TRANSCRIPT_EVENT from JS
                timer = get_speech_timer()
                if timer and not timer.done():
                    timer.cancel()
                new_timer = asyncio.create_task(_delayed_flush(interview_id, 0.5))
                set_speech_timer(new_timer)
                return

            # Final transcript from JS silence timer
            if text.startswith('TRANSCRIPT_EVENT:'):
                final_text = text.split('TRANSCRIPT_EVENT:', 1)[1].strip()
                if final_text:
                    # Accumulate (candidate may speak in multiple segments)
                    buffer = get_speech_buffer()
                    buffer = (buffer + " " + final_text).strip() if buffer else final_text
                    set_speech_buffer(buffer)
                    update_last_speech_time()

                    msg_log = f"🎤 [STT FINAL] \"{final_text[:80]}\""
                    print(msg_log); await push_log(msg_log)

                    # FIX: Don't flush immediately — let JS silence timer handle batching.
                    # Start a delayed flush as safety net (2s) in case SPEECH_END never fires.
                    timer = get_speech_timer()
                    if timer and not timer.done():
                        timer.cancel()
                    new_timer = asyncio.create_task(_delayed_flush(interview_id, 2.0))
                    set_speech_timer(new_timer)
                return

            # Log STT errors only — STT_ACTIVE: fires every restart (~8s) and is just noise
            elif text.startswith('STT_ERROR:') or text.startswith('STT_LOW_CONF:'):
                msg_log = f"🎤 {text[:200]}"
                print(msg_log); await push_log(msg_log)

        except Exception as e:
            print(f"Console handler error: {e}")

    page.on('console', _handle_console)
    return _handle_console
