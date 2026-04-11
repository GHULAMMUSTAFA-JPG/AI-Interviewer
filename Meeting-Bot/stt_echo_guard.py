"""
STT Echo Guard — watches bot_speaking flag to gate STT during TTS playback.

Uses TWO mechanisms for robust echo prevention:
1. Redis pub/sub (instant, ~10-50ms): TTS publishes tts_status → we gate immediately
2. MongoDB change stream (fallback, ~1-6s): catches Redis misses

When bot_speaking becomes True:
    → Call __tts_started() in browser (discards STT events)
When bot_speaking becomes False:
    → Call __tts_ended(wasInterrupted) in browser
      (resumes STT, with or without echo gate)
"""
import asyncio
import json
from logger import push_log


async def create_echo_guard_watcher(db, interview_id: str, status_state: dict,
                                     page, mongo_connected: bool = False):
    """
    Watch for bot speaking state changes using Redis + MongoDB.

    Redis pub/sub gives instant notification (~10-50ms) when TTS starts/stops.
    MongoDB change stream is a fallback for when Redis is unavailable.
    """
    # Start Redis watcher as primary (fast)
    redis_task = None
    try:
        from redis_client import get_redis
        redis_task = asyncio.create_task(
            _watch_tts_status_redis(interview_id, status_state, page)
        )
    except Exception as e:
        msg = f"Redis echo watcher unavailable: {e} — using MongoDB fallback"
        print(msg); await push_log(msg)

    # Start MongoDB watcher as fallback
    mongo_task = None
    if mongo_connected and db is not None:
        mongo_task = asyncio.create_task(
            _watch_bot_speaking_mongo(db, interview_id, status_state, page)
        )

    # Return a wrapper that can cancel both
    async def _combined_watcher():
        tasks = [t for t in (redis_task, mongo_task) if t is not None]
        if not tasks:
            msg = "Bot speaking watcher skipped — no Redis or MongoDB available"
            print(msg); await push_log(msg)
            return

        msg = f"Echo guard active: Redis={redis_task is not None}, MongoDB={mongo_task is not None}"
        print(msg); await push_log(msg)

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception():
                msg = f"Echo guard task failed: {t.exception()}"
                print(msg); await push_log(msg)
        # Keep remaining tasks running
        for t in pending:
            t.cancel()

    return asyncio.create_task(_combined_watcher())


async def _watch_tts_status_redis(interview_id: str, status_state: dict, page):
    """
    Primary echo guard: subscribe to Redis TTS status channel.

    TTS service publishes tts_status events when it starts/stops speaking.
    This gives ~10-50ms latency vs MongoDB change stream's 1-6 seconds.
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
            msg = f"🔴 Echo guard: subscribed to Redis channel {channel}"
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
                        msg = "🔇 Bot speaking (Redis) — clearing buffer + gating STT"
                        print(msg); await push_log(msg)
                        from stt_speech_buffer import clear_speech_buffer
                        clear_speech_buffer()
                        try:
                            await page.evaluate("window.__tts_started()")
                        except Exception as eval_err:
                            msg = f"__tts_started eval error: {eval_err}"
                            print(msg); await push_log(msg)
                    else:
                        # Check if there was an interrupt
                        was_interrupted = data.get("interrupted", False)
                        gate_label = "no echo gate (interrupted)" if was_interrupted else "echo gate active"
                        msg = f"🔊 Bot done speaking (Redis) — resuming STT ({gate_label})"
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
    Slower (1-6s) but reliable even if Redis pub/sub fails.
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
            msg = "Bot speaking watcher (MongoDB fallback): change stream opened"
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
                            msg = "🔇 Bot speaking (MongoDB fallback) — clearing buffer + gating STT"
                            print(msg); await push_log(msg)
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
                            msg = f"🔊 Bot done speaking (MongoDB fallback) — resuming STT ({gate_label})"
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
