"""
STT Echo Guard — watches bot_speaking flag to gate STT during TTS playback.

PRIMARY: Redis pub/sub (~10-50ms).
FALLBACK: MongoDB change stream (~1-6s), only activated if Redis is unavailable
          or crashes — never runs simultaneously with Redis.

When bot_speaking becomes True:
    → Wait 500ms (matches interrupt_handler block window) then clear buffer
    → Call __tts_started() in browser (discards STT events)
When bot_speaking becomes False:
    → Call __tts_ended(wasInterrupted) in browser
      (resumes STT, with or without echo gate)
"""
import asyncio
import json
from logger import push_log

# Must match interrupt_handler.INTERRUPT_BLOCK_WINDOW_SEC (0.5s).
# Any candidate speech recorded in this window before the bot starts
# is legitimate — delaying the buffer clear preserves it.
_BUFFER_CLEAR_DELAY_SEC = 0.5


async def _safe_evaluate(page, expr: str, label: str, max_attempts: int = 3) -> bool:
    """
    Call page.evaluate() with retry.
    A single JS context error should not permanently disable echo protection.
    Returns True on success, False if all attempts fail.
    """
    for attempt in range(max_attempts):
        try:
            await page.evaluate(expr)
            return True
        except Exception as e:
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.2)
            else:
                await push_log(f"{label} eval failed after {max_attempts} attempts: {e}")
    return False


async def create_echo_guard_watcher(db, interview_id: str, status_state: dict,
                                     page, mongo_connected: bool = False):
    """
    Watch for bot speaking state changes — Redis primary, MongoDB fallback only.

    Tries Redis first. If Redis is unavailable or crashes mid-session, falls back
    to the MongoDB change stream. They never run simultaneously, preventing the
    double __tts_started() / __tts_ended() race that wiped interrupt state.
    """
    async def _run():
        redis_ok = False
        try:
            from redis_client import get_redis
            await get_redis()
            redis_ok = True
        except Exception as e:
            msg = f"Redis echo guard unavailable: {e} — using MongoDB fallback"
            await push_log(msg)

        if redis_ok:
            msg = "Echo guard: Redis primary active"
            await push_log(msg)
            try:
                await _watch_tts_status_redis(interview_id, status_state, page)
                return  # clean shutdown
            except asyncio.CancelledError:
                return
            except Exception as e:
                msg = f"Redis echo guard crashed: {e} — activating MongoDB fallback"
                await push_log(msg)

        # Redis unavailable or crashed — MongoDB fallback
        if mongo_connected and db is not None:
            msg = "Echo guard: MongoDB fallback active"
            await push_log(msg)
            try:
                await _watch_bot_speaking_mongo(db, interview_id, status_state, page)
            except asyncio.CancelledError:
                return
            except Exception as e:
                msg = f"MongoDB echo guard failed: {e}"
                await push_log(msg)
        else:
            msg = "Echo guard skipped — no Redis or MongoDB available"
            await push_log(msg)

    return asyncio.create_task(_run())


async def _watch_tts_status_redis(interview_id: str, status_state: dict, page):
    """
    Primary echo guard: subscribe to Redis TTS status channel.

    TTS service publishes tts_status events when it starts/stops speaking.
    This gives ~10-50ms latency vs MongoDB change stream's 1-6 seconds.

    Buffer clear is delayed by _BUFFER_CLEAR_DELAY_SEC so that candidate speech
    recorded just before the bot starts is not silently dropped.
    """
    retry_delay = 1.0
    while True:
        try:
            from redis_client import get_redis
            redis = await get_redis()
            pubsub = redis.pubsub()
            channel = f"tts:{interview_id}:status_events"
            cancel_channel = f"tts:{interview_id}:interrupt_cancel"
            arm_ready_channel = f"tts:{interview_id}:arm_ready"
            guard_ready_channel = f"tts:{interview_id}:guard_ready"
            await pubsub.subscribe(channel, cancel_channel, arm_ready_channel)

            retry_delay = 1.0
            msg = f"Echo guard: subscribed to Redis channel {channel}"
            await push_log(msg)

            # Startup state sync: read current TTS status from Redis hash before
            # listening for pub/sub events. Without this, if TTS was already speaking
            # when we subscribed (or after a reconnect), we'd miss the "speaking" event
            # and JS would never gate STT — bot's own voice leaks through as candidate speech.
            try:
                current_status = await redis.hget(f"tts:{interview_id}:status", "status")
                if current_status:
                    current_is_speaking = current_status.decode() == "speaking"
                    if current_is_speaking != status_state.get("bot_speaking", False):
                        status_state["bot_speaking"] = current_is_speaking
                        if current_is_speaking:
                            from stt_speech_buffer import clear_speech_buffer
                            clear_speech_buffer()
                            await _safe_evaluate(page, "window.__tts_started()", "__tts_started (startup sync)")
                            await push_log("Echo guard: startup sync — bot was already speaking")
                        else:
                            await _safe_evaluate(page, "window.__tts_ended(false)", "__tts_ended (startup sync)")
                            await push_log("Echo guard: startup sync — bot was idle")
            except Exception as sync_err:
                await push_log(f"Echo guard startup sync failed (non-fatal): {sync_err}")

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    msg_channel = message.get("channel", b"")
                    if isinstance(msg_channel, bytes):
                        msg_channel = msg_channel.decode()

                    # 4.1: arm_ready — TTS just armed; call __tts_started() and publish guard_ready
                    if msg_channel == arm_ready_channel:
                        # 2.2: Set dynamic echo gate from TTS-published audio duration
                        try:
                            dur_bytes = await redis.get(f"tts:{interview_id}:audio_duration_ms")
                            if dur_bytes:
                                dur_ms = int(dur_bytes.decode())
                                await _safe_evaluate(
                                    page,
                                    f"window.__set_echo_gate && window.__set_echo_gate({dur_ms})",
                                    "__set_echo_gate"
                                )
                        except Exception:
                            pass
                        await _safe_evaluate(page, "window.__tts_started()", "__tts_started (arm_ready)")
                        try:
                            await redis.publish(guard_ready_channel, "1")
                        except Exception:
                            pass
                        await push_log("Echo guard: arm_ready → echo gate set, __tts_started() called, guard_ready published")
                        continue

                    # P6: interrupt_cancel — TTS confirmed noise, not real speech.
                    # Discard words buffered during the false-positive window.
                    if msg_channel == cancel_channel:
                        await _safe_evaluate(
                            page,
                            "window.__clear_interrupt_accumulation && window.__clear_interrupt_accumulation()",
                            "__clear_interrupt_accumulation"
                        )
                        await push_log("Echo guard: interrupt_cancel received — cleared accumulation buffer")
                        continue

                    data = json.loads(message["data"])
                    status = data.get("status")  # "speaking" or "idle"
                    is_speaking = status == "speaking"

                    if is_speaking == status_state.get("bot_speaking", False):
                        continue

                    status_state["bot_speaking"] = is_speaking

                    if is_speaking:
                        from stt_speech_buffer import clear_speech_buffer
                        try:
                            buf_bytes = await redis.get(f"speech:{interview_id}:buffer")
                            buf_content = buf_bytes.decode() if buf_bytes else ""
                        except Exception:
                            buf_content = ""
                        if buf_content.strip():
                            # Delay only when buffer has content — preserve words spoken
                            # just before the bot starts during this window.
                            msg = "Bot speaking (Redis) — buffer non-empty, delaying clear + gating STT"
                            await push_log(msg)
                            await asyncio.sleep(_BUFFER_CLEAR_DELAY_SEC)
                        else:
                            msg = "Bot speaking (Redis) — buffer empty, gating STT immediately"
                            await push_log(msg)
                        await clear_speech_buffer(redis, interview_id)
                        await _safe_evaluate(page, "window.__tts_started()", "__tts_started")
                    else:
                        was_interrupted = data.get("interrupted", False)
                        gate_label = "no echo gate (interrupted)" if was_interrupted else "echo gate active"
                        msg = f"Bot done speaking (Redis) — resuming STT ({gate_label})"
                        await push_log(msg)
                        js_flag = "true" if was_interrupted else "false"
                        await _safe_evaluate(page, f"window.__tts_ended({js_flag})", "__tts_ended")

                except (json.JSONDecodeError, KeyError) as parse_err:
                    msg = f"Redis message parse error: {parse_err}"
                    await push_log(msg)

        except asyncio.CancelledError:
            return
        except Exception as e:
            msg = f"Redis echo guard error (retry in {retry_delay}s): {e}"
            await push_log(msg)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)


async def _watch_bot_speaking_mongo(db, interview_id: str, status_state: dict, page):
    """
    Fallback echo guard: watch MongoDB for bot_speaking changes.
    Only runs when Redis is completely unavailable. Slower (1-6s) but reliable.
    """
    pipeline = [
        {
            "$match": {
                "operationType": "update",
                "fullDocument.interview_id": interview_id,
            }
        }
    ]

    retry_delay = 1.0
    while True:
        try:
            msg = "Echo guard (MongoDB fallback): change stream opened"
            await push_log(msg)
            async with db.interviews.watch(pipeline, full_document="updateLookup") as stream:
                retry_delay = 1.0
                async for change in stream:
                    try:
                        full_doc = change.get("fullDocument") or {}
                        is_speaking = full_doc.get("bot_speaking", False)

                        if is_speaking == status_state.get("bot_speaking", False):
                            continue

                        status_state["bot_speaking"] = is_speaking

                        if is_speaking:
                            msg = "Bot speaking (MongoDB fallback) — delaying buffer clear + gating STT"
                            await push_log(msg)

                            # Same delay as Redis path for consistency
                            await asyncio.sleep(_BUFFER_CLEAR_DELAY_SEC)

                            from stt_speech_buffer import clear_speech_buffer
                            clear_speech_buffer()
                            await _safe_evaluate(page, "window.__tts_started()", "__tts_started (MongoDB fallback)")
                        else:
                            # Always pass wasInterrupted=false on the MongoDB fallback path.
                            # Reading tts_interrupt from the document is unreliable — the flag
                            # may be stale from a previous turn since disarm() does not clear it.
                            # Passing false is the safe default: echo gate runs, no candidate words
                            # are lost (candidate would need to wait the echo gate period, ~700ms).
                            msg = "Bot done speaking (MongoDB fallback) — resuming STT (echo gate active)"
                            await push_log(msg)
                            await _safe_evaluate(page, "window.__tts_ended(false)", "__tts_ended (MongoDB fallback)")
                    except Exception as inner_err:
                        msg = f"MongoDB echo watcher inner error: {inner_err}"
                        await push_log(msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            msg = f"MongoDB echo guard error (retry in {retry_delay}s): {e}"
            await push_log(msg)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)
