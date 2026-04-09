"""
STT Console Handler — captures Web Speech API events from browser console.

Handles:
- Parsing STT events (interim, speech start/end, final transcripts)
- Triggering interrupt when candidate speaks during bot
- Accumulating speech segments in buffer
- Deferred flush (500ms after SPEECH_END, 2s safety net)
"""
import asyncio
from logger import push_log
from stt_speech_buffer import (
    get_speech_buffer, set_speech_buffer, clear_speech_buffer,
    get_speech_timer, set_speech_timer, update_last_speech_time,
    _delayed_flush
)


async def create_console_handler(page, interview_id: str, status_state: dict,
                                  db=None, mongo_connected: bool = False):
    """
    Create and attach console event handler to capture Web Speech API transcripts.

    Args:
        page: Playwright page object
        interview_id: Current interview ID
        status_state: Dict with {"bot_speaking": bool}
        db: MongoDB database instance
        mongo_connected: Whether MongoDB is connected
    """

    async def _handle_console(msg):
        try:
            text = msg.text

            # ─── Interim Results ───────────────────────────────────
            if text.startswith('STT_INTERIM:'):
                interim_text = text.split('STT_INTERIM:', 1)[1].strip()
                if interim_text:
                    print(f"[INTERIM] {interim_text[:120]}")
                return

            # ─── Speech Start ──────────────────────────────────────
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
                            msg_log = "[INTERRUPT] Candidate interrupted bot"
                            print(msg_log); await push_log(msg_log)
                        except Exception as int_err:
                            print(f"Interrupt signal error: {int_err}")
                else:
                    msg_log = "[SPEECH STARTED] Candidate speaking..."
                    print(msg_log); await push_log(msg_log)
                return

            # ─── Speech End ────────────────────────────────────────
            if text.startswith('STT_SPEECH_END:'):
                msg_log = "[SPEECH ENDED] Waiting for final transcript..."
                print(msg_log); await push_log(msg_log)

                # Flush after 500ms to catch any last TRANSCRIPT_EVENT from JS
                timer = get_speech_timer()
                if timer and not timer.done():
                    timer.cancel()
                new_timer = asyncio.create_task(_delayed_flush(interview_id, 0.5))
                set_speech_timer(new_timer)
                return

            # ─── Final Transcript ──────────────────────────────────
            if text.startswith('TRANSCRIPT_EVENT:'):
                final_text = text.split('TRANSCRIPT_EVENT:', 1)[1].strip()
                if final_text:
                    # Accumulate (candidate may speak in multiple segments)
                    buffer = get_speech_buffer()
                    buffer = (buffer + " " + final_text).strip() if buffer else final_text
                    set_speech_buffer(buffer)
                    update_last_speech_time()

                    msg_log = f"[STT FINAL] \"{final_text[:80]}\""
                    print(msg_log); await push_log(msg_log)

                    # Start a delayed flush as safety net (2s) in case SPEECH_END never fires
                    timer = get_speech_timer()
                    if timer and not timer.done():
                        timer.cancel()
                    new_timer = asyncio.create_task(_delayed_flush(interview_id, 2.0))
                    set_speech_timer(new_timer)
                return

            # ─── STT Errors ────────────────────────────────────────
            if text.startswith('STT_ERROR:') or text.startswith('STT_LOW_CONF:'):
                msg_log = f"[STT] {text[:200]}"
                print(msg_log); await push_log(msg_log)

        except Exception as e:
            print(f"Console handler error: {e}")

    page.on('console', _handle_console)
    return _handle_console
