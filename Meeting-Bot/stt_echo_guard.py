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
            print(msg); await push_log(msg)

        if redis_ok:
            msg = "Echo guard: Redis primary active"
            print(msg); await push_log(msg)
            try:
                await _watch_tts_status_redis(interview_id, status_state, page)
                return  # clean shutdown
            except asyncio.CancelledError:
                return
            except Exception as e:
                msg = f"Redis echo guard crashed: {e} — activating MongoDB fallback"
                print(msg); await push_log(msg)

        # Redis unavailable or crashed — MongoDB fallback
        if mongo_connected and db is not None:
            msg = "Echo guard: MongoDB fallback active"
            print(msg); await push_log(msg)
            try:
                await _watch_bot_speaking_mongo(db, interview_id, status_state, page)
            except asyncio.CancelledError:
                return
            except Exception as e:
                msg = f"MongoDB echo guard failed: {e}"
                print(msg); await push_log(msg)
        else:
            msg = "Echo guard skipped — no Redis or MongoDB available"
            print(msg); await push_log(msg)

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
            await pubsub.subscribe(channel)

            retry_delay = 1.0
            msg = f"Echo guard: subscribed to Redis channel {channel}"
            print(msg); await push_log(msg)

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    status = data.get("status")  # "speaking" or "idle"
                    is_speaking = status == "speaking"

                    if is_speaking == status_state.get("bot_speaking", False):
                        continue

                    status_state["bot_speaking"] = is_speaking

                    if is_speaking:
                        msg = "Bot speaking (Redis) — delaying buffer clear + gating STT"
                        print(msg); await push_log(msg)

                        # Delay matches interrupt_handler block window (500ms).
                        # Candidate words spoken just before the bot starts are
                        # preserved in the buffer during this window.
                        await asyncio.sleep(_BUFFER_CLEAR_DELAY_SEC)

                        from stt_speech_buffer import clear_speech_buffer
                        clear_speech_buffer()
                        try:
                            await page.evaluate("window.__tts_started()")
                        except Exception as eval_err:
                            msg = f"__tts_started eval error: {eval_err}"
                            print(msg); await push_log(msg)
                    else:
                        was_interrupted = data.get("interrupted", False)
                        gate_label = "no echo gate (interrupted)" if was_interrupted else "echo gate active"
                        msg = f"Bot done speaking (Redis) — resuming STT ({gate_label})"
                        print(msg); await push_log(msg)
                        try:
                            js_flag = "true" if was_interrupted else "false"
                            await page.evaluate(f"window.__tts_ended({js_flag})")
                        except Exception as eval_err:
                            msg = f"__tts_ended eval error: {eval_err}"
                            print(msg); await push_log(msg)

                except (json.JSONDecodeError, KeyError) as parse_err:
                    msg = f"Redis message parse error: {parse_err}"
                    print(msg); await push_log(msg)

        except asyncio.CancelledError:
            return
        except Exception as e:
            msg = f"Redis echo guard error (retry in {retry_delay}s): {e}"
            print(msg); await push_log(msg)
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
            print(msg); await push_log(msg)
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
                            print(msg); await push_log(msg)

                            # Same delay as Redis path for consistency
                            await asyncio.sleep(_BUFFER_CLEAR_DELAY_SEC)

                            from stt_speech_buffer import clear_speech_buffer
                            clear_speech_buffer()
                            try:
                                await page.evaluate("window.__tts_started()")
                            except Exception as eval_err:
                                msg = f"__tts_started eval error: {eval_err}"
                                print(msg); await push_log(msg)
                        else:
                            was_interrupted = bool(full_doc.get("tts_interrupt", False))
                            gate_label = "no echo gate (interrupted)" if was_interrupted else "echo gate active"
                            msg = f"Bot done speaking (MongoDB fallback) — resuming STT ({gate_label})"
                            print(msg); await push_log(msg)
                            try:
                                js_flag = "true" if was_interrupted else "false"
                                await page.evaluate(f"window.__tts_ended({js_flag})")
                            except Exception as eval_err:
                                msg = f"__tts_ended eval error: {eval_err}"
                                print(msg); await push_log(msg)
                    except Exception as inner_err:
                        msg = f"MongoDB echo watcher inner error: {inner_err}"
                        print(msg); await push_log(msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            msg = f"MongoDB echo guard error (retry in {retry_delay}s): {e}"
            print(msg); await push_log(msg)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)
