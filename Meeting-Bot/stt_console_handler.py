"""
STT Console Handler — captures Web Speech API events from browser console.

Handles:
- Parsing STT events (interim, speech start/end, final transcripts)
- Triggering interrupt via Redis pub/sub (primary) + MongoDB (parallel)
- Accumulating speech segments in per-interview Redis buffer
- Deferred flush (VAD-based, 300ms after speech end)
"""
import asyncio
import json
import time
from logger import push_log
from stt_speech_buffer import (
    append_speech, clear_speech_buffer, reset_session,
    _delayed_flush, cancel_pending_flush
)


async def create_console_handler(page, interview_id: str, status_state: dict,
                                  redis=None, db=None, mongo_connected: bool = False):
    """
    Create and attach console event handler to capture Web Speech API transcripts.

    Args:
        page: Playwright page object
        interview_id: Current interview ID
        status_state: Dict with {"bot_speaking": bool}
        redis: Redis client (for speech buffer + interrupt pub/sub)
        db: MongoDB database instance
        mongo_connected: Whether MongoDB is connected
    """
    _request_id = 0
    _interrupt_signal_pending = False  # True while optimistic countdown is active

    def _new_request_id():
        nonlocal _request_id
        _request_id += 1
        return f"req-{interview_id[:8]}-{_request_id}"

    async def _publish_interrupt_signal():
        """Publish interrupt_signal immediately on onspeechstart (Redis-first optimistic path).

        TTS starts a 400ms countdown. If interrupt_confirm arrives within 400ms, audio stops.
        If no confirm, TTS publishes interrupt_cancel and resumes seamlessly.
        MongoDB write runs in parallel as fallback for when Redis is unavailable.
        """
        nonlocal _interrupt_signal_pending
        _interrupt_signal_pending = True

        if redis:
            try:
                await redis.publish(
                    f"tts:{interview_id}:interrupt_signal",
                    json.dumps({"request_id": _new_request_id(), "timestamp": time.time()})
                )
            except Exception as e:
                print(f"Redis interrupt_signal publish error: {e}")
                _interrupt_signal_pending = False

        # MongoDB parallel write — fallback path for Redis-unavailable scenarios
        if mongo_connected and db is not None:
            try:
                await db.interviews.update_one(
                    {"interview_id": interview_id},
                    {"$set": {"tts_interrupt": True}}
                )
            except Exception:
                pass

    async def _publish_interrupt_confirm():
        """Publish interrupt_confirm when real speech is detected (first onresult).

        This resolves the 400ms countdown in TTS, causing immediate audio stop.
        """
        nonlocal _interrupt_signal_pending
        if not _interrupt_signal_pending:
            return
        _interrupt_signal_pending = False

        if redis:
            try:
                await redis.publish(
                    f"tts:{interview_id}:interrupt_confirm",
                    json.dumps({"timestamp": time.time()})
                )
            except Exception as e:
                print(f"Redis interrupt_confirm publish error: {e}")

    async def _handle_console(msg):
        try:
            text = msg.text

            # ─── Interim Results ───────────────────────────────────
            if text.startswith('STT_INTERIM:'):
                raw = text.split('STT_INTERIM:', 1)[1].strip()
                try:
                    interim_text = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    interim_text = raw
                if interim_text:
                    print(f"[INTERIM] {interim_text[:120]}")
                return

            # ─── Speech Start ──────────────────────────────────────
            if text.startswith('STT_SPEECH_START:'):
                if redis:
                    from stt_speech_buffer import update_last_speech_time
                    update_last_speech_time(interview_id)

                if status_state["bot_speaking"]:
                    # Candidate is cutting in — clear stale buffer and fire interrupt_signal.
                    # Optimistic: fire immediately, confirm or cancel within 400ms.
                    cancel_pending_flush(interview_id)
                    if redis:
                        await clear_speech_buffer(redis, interview_id)

                    asyncio.create_task(_publish_interrupt_signal())
                    await push_log("[INTERRUPT SIGNAL] Candidate audio detected — 400ms countdown started")
                else:
                    await push_log("[SPEECH STARTED] Candidate speaking...")
                return

            # ─── VAD Speech End ────────────────────────────────────
            if text.startswith('STT_VAD_SPEECH_END:'):
                # VAD detected speech ended (300ms silence) — flush immediately
                cancel_pending_flush(interview_id)
                if redis:
                    from stt_speech_buffer import _flush_speech_buffer
                    asyncio.create_task(_flush_speech_buffer(redis, interview_id))
                await push_log("[VAD END] Speech ended — flushing buffer")
                return

            # ─── Speech End (fallback) ─────────────────────────────
            if text.startswith('STT_SPEECH_END:'):
                # Fallback: onspeechend fired but VAD may have already handled it
                cancel_pending_flush(interview_id)
                if redis:
                    from stt_speech_buffer import _flush_speech_buffer
                    # Short delay to catch any trailing TRANSCRIPT_EVENT
                    new_timer = asyncio.create_task(_delayed_flush(redis, interview_id, 0.3))
                    from stt_speech_buffer import _speech_timers
                    _speech_timers[interview_id] = new_timer
                return

            # ─── Final Transcript ──────────────────────────────────
            if text.startswith('TRANSCRIPT_EVENT:'):
                raw = text.split('TRANSCRIPT_EVENT:', 1)[1].strip()
                try:
                    final_text = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    final_text = raw
                if final_text:
                    # Real speech confirmed — resolve optimistic countdown (interrupt_confirm).
                    # This fires ~50ms after interrupt_signal, stopping audio immediately.
                    if _interrupt_signal_pending:
                        asyncio.create_task(_publish_interrupt_confirm())
                        await push_log("[INTERRUPT CONFIRM] Real speech confirmed — stopping audio")

                    if redis:
                        await append_speech(redis, interview_id, final_text)
                        from stt_speech_buffer import update_last_speech_time
                        update_last_speech_time(interview_id)

                    msg_log = f"[STT FINAL] \"{final_text[:80]}\""
                    await push_log(msg_log)
                return

            # ─── STT Errors ────────────────────────────────────────
            if text.startswith('STT_ERROR:') or text.startswith('STT_LOW_CONF:'):
                msg_log = f"[STT] {text[:200]}"
                await push_log(msg_log)

        except Exception as e:
            print(f"Console handler error: {e}")

    page.on('console', _handle_console)
    return _handle_console
