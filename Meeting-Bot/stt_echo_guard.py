"""
STT Echo Guard — watches bot_speaking flag to gate STT during TTS playback.

Handles:
- Watching MongoDB for bot_speaking changes
- Calling __tts_started() when bot starts speaking
- Calling __tts_ended(wasInterrupted) when bot stops
- Auto-reconnect on change stream errors
"""
import asyncio
from logger import push_log


async def create_echo_guard_watcher(db, interview_id: str, status_state: dict,
                                     page, mongo_connected: bool = False):
    """
    Watch bot_speaking flag for echo cancellation.

    When bot_speaking becomes True:
        → Call __tts_started() in browser (discards STT events)
    When bot_speaking becomes False:
        → Call __tts_ended(wasInterrupted) in browser
          (resumes STT, with or without echo gate)
    """
    if not mongo_connected or db is None:
        msg = "Bot speaking watcher skipped -- MongoDB not connected"
        print(msg); await push_log(msg)
        return None

    pipeline = [
        {
            "$match": {
                "operationType": "update",
                "fullDocument.interview_id": interview_id,
            }
        }
    ]

    async def _watch_bot_speaking():
        retry_delay = 1.0
        while True:
            try:
                msg = "Bot speaking watcher: change stream opened"
                print(msg); await push_log(msg)
                async with db.interviews.watch(pipeline, full_document="updateLookup") as stream:
                    retry_delay = 1.0
                    async for change in stream:
                        try:
                            full_doc = change.get("fullDocument") or {}
                            is_speaking = full_doc.get("bot_speaking", False)

                            if is_speaking == status_state["bot_speaking"]:
                                continue

                            status_state["bot_speaking"] = is_speaking

                            if is_speaking:
                                msg = "Bot speaking -- clearing buffer + gating STT"
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
                                msg = f"Bot done speaking -- resuming STT ({gate_label})"
                                print(msg); await push_log(msg)
                                try:
                                    js_flag = "true" if was_interrupted else "false"
                                    await page.evaluate(f"window.__tts_ended({js_flag})")
                                except Exception as eval_err:
                                    msg = f"__tts_ended eval error: {eval_err}"
                                    print(msg); await push_log(msg)
                        except Exception as inner_err:
                            msg = f"Bot speaking watcher inner error: {inner_err}"
                            print(msg); await push_log(msg)
            except asyncio.CancelledError:
                return
            except Exception as e:
                msg = f"Bot speaking watcher error (retry in {retry_delay}s): {e}"
                print(msg); await push_log(msg)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)

    return asyncio.create_task(_watch_bot_speaking())
