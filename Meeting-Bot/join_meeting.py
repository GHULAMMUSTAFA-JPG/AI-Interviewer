"""
Join Meeting Orchestrator — coordinates all meeting bot components.

This is the main entry point for joining a Google Meet session.
It orchestrates browser lifecycle, media controls, speech capture,
and meeting monitoring.
"""
import asyncio
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from logger import push_log
from mongo_handler import connect_to_mongo, disconnect_from_mongo, insert_transcript
import mongo_handler
from stt_injector import inject_speech_recognition
from media_controls import ensure_mic_on, disable_camera
from state_manager import set_bot_state
from meeting_watcher import watch_for_leave
from stt_speech_buffer import (
    init_speech_state, reset_session, cancel_pending_flush, get_last_speech_time
)
from stt_console_handler import create_console_handler
from stt_echo_guard import create_echo_guard_watcher
from stt_audio_router import create_audio_router, setup_audio_routing_after_admission

# Load environment variables
load_dotenv()
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en-US")


async def join_meeting_and_transcribe(
    url: str,
    email: str,
    password: str = "",
    headless: bool = False,
    interview_id: str = None,
) -> None:
    """
    Join Google Meet as guest and transcribe via Web Speech API (BATCH mode).

    Flow:
        1. Launch Chromium
        2. Navigate to meeting URL
        3. Enter guest name
        4. Pre-join: disable camera, ensure mic ON
        5. Click 'Join'
        6. Wait for host to admit
        7. Inject Web Speech API
        8. Capture ALL speech during meeting (buffer locally)
        9. When meeting ends: insert ONE final transcript
    """
    init_speech_state()  # Reset speech state (no-op, kept for clarity)

    temp_dir = tempfile.mkdtemp(prefix="meet_bot_")
    session_id = interview_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    name = email.split("@")[0]

    msg = f"🤖 Bot: {name}  [WEB SPEECH API]  lang={STT_LANGUAGE}  session={session_id}"
    await push_log(msg)

    # Pre-grant microphone permission
    _prefs_dir = Path(temp_dir) / "Default"
    _prefs_dir.mkdir(parents=True, exist_ok=True)
    _prefs = {
        "profile": {
            "content_settings": {
                "exceptions": {
                    "media_stream_mic": {
                        "https://meet.google.com:443,*": {
                            "last_modified": "13369071600000000",
                            "setting": 1
                        }
                    }
                }
            }
        }
    }
    (_prefs_dir / "Preferences").write_text(str(_prefs).replace("'", '"'))

    mongo_connected = await connect_to_mongo()
    db = mongo_handler.db

    if not mongo_connected:
        msg = "⚠️  Continuing without MongoDB (local transcripts only)"
        await push_log(msg)

    await set_bot_state(interview_id, "joining", db, mongo_connected)

    async with async_playwright() as p:
        try:
            # Remove stale Chrome lock files
            chrome_profile_dir = "/app/chrome_profile"
            for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
                lock_file = Path(chrome_profile_dir) / lock_name
                if lock_file.exists() or lock_file.is_symlink():
                    try:
                        lock_file.unlink()
                        msg = f"🔧 Removed stale Chrome lock file: {lock_name}"
                        await push_log(msg)
                    except Exception as e:
                        msg = f"⚠️  Failed to remove {lock_name}: {e} (will try anyway)"
                        await push_log(msg)

            msg = "🌐 Launching Chromium (with persistent profile)..."
            await push_log(msg)

            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=chrome_profile_dir,
                headless=False,
                channel="chrome",
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--autoplay-policy=no-user-gesture-required",
                    "--use-fake-ui-for-media-stream",
                    "--allow-running-insecure-content",
                    "--disable-features=BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-position=0,0",
                    "--disable-features=AudioServiceSandbox,AudioServiceOutOfProcess",
                    "--alsa-output-device=pulse",
                    "--alsa-input-device=pulse",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--disable-sync",
                    "--no-first-run",
                    "--start-maximized",
                ],
                accept_downloads=False,
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1920, "height": 1080},
            )

            msg = f"✅ Chrome launched with profile: {chrome_profile_dir}"
            await push_log(msg)

            await ctx.grant_permissions(["microphone"], origin="https://meet.google.com")

            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            # Track bot speaking status for interruption and echo cancellation
            status_state = {"bot_speaking": False}

            # Get Redis client for per-interview speech buffer + interrupt pub/sub
            from redis_client import get_redis
            _redis = None
            try:
                _redis = await get_redis()
            except Exception as rerr:
                await push_log(f"Redis unavailable for speech buffer: {rerr}")

            if _redis:
                await reset_session(_redis, interview_id)

            # Create console handler for STT events (Redis-backed speech buffer)
            await create_console_handler(
                page, interview_id, status_state, _redis, db, mongo_connected
            )
            page.on('pageerror', lambda err: print(f"[PAGE ERROR] {err}"))

            async def _on_page_navigated(frame):
                """Detect when Chrome navigates away from Google Meet — host removed bot."""
                if frame != page.main_frame:
                    return
                url = page.url
                if "meet.google.com" not in url and url not in ("about:blank", ""):
                    msg = f"🚨 Page navigated away from Meet → {url} — bot was removed"
                    await push_log(msg)
                    try:
                        await page.evaluate("window.__stop_interview()")
                    except Exception:
                        pass
                    if mongo_connected and db is not None:
                        try:
                            await db.interviews.update_one(
                                {"interview_id": interview_id},
                                {"$set": {"status": "abandoned", "ended_at": datetime.utcnow(),
                                          "abandon_reason": "host_removed_bot"}},
                            )
                        except Exception:
                            pass
                    await set_bot_state(interview_id, "abandoned", db, mongo_connected)

            page.on('framenavigated', _on_page_navigated)

            # Inject device-change protection
            await page.add_init_script("""
(function blockDeviceChange() {
    var _origAdd = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function(type, listener, options) {
        if (this === navigator.mediaDevices && type === 'devicechange') {
            console.log('[BOT-INIT] devicechange listener BLOCKED');
            return;
        }
        return _origAdd.call(this, type, listener, options);
    };
    try {
        Object.defineProperty(navigator.mediaDevices, 'ondevicechange', {
            get: function() { return null; },
            set: function() { console.log('[BOT-INIT] ondevicechange setter BLOCKED'); },
            configurable: true
        });
    } catch(e) {}
    console.log('[BOT-INIT] Audio device-change protection active');
})();
""")
            await push_log("✅ [INIT] devicechange protection injected")

            # Navigate to meeting
            msg = f"🌐 Navigating → {url}"
            await push_log(msg)
            try:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                msg = f"❌ Navigation failed: {e}"
                await push_log(msg)
                await ctx.close()
                return

            await page.wait_for_timeout(5000)

            # Enter guest name
            for selector in [
                "input[placeholder='Your name']",
                "input[aria-label='Your name']",
            ]:
                try:
                    if await page.locator(selector).count() > 0:
                        await page.locator(selector).first.fill(f"{name} (AI)")
                        await page.wait_for_timeout(1000)
                        msg = "✅ Guest name entered"
                        await push_log(msg)
                        break
                except Exception:
                    pass

            # Pre-join: ensure mic ON + disable camera
            await ensure_mic_on(page)
            await disable_camera(page)

            # Click join button
            msg = "🔍 Clicking join button..."
            await push_log(msg)
            joined = False
            for _ in range(15):
                for txt in ["Ask to join", "Join now", "Join"]:
                    for tag in ["button", "span", "div"]:
                        try:
                            loc = page.locator(f"{tag}:has-text('{txt}')")
                            if await loc.count() > 0 and await loc.first.is_visible():
                                await loc.first.click()
                                joined = True
                                break
                        except Exception:
                            pass
                    if joined:
                        break
                if joined:
                    break
                await page.wait_for_timeout(1000)

            if not joined:
                msg = "❌ No join button found — aborting"
                await push_log(msg)
                await ctx.close()
                return

            # Wait for admittance
            msg = "⏳ Waiting for host to admit (up to 10 min)..."
            await push_log(msg)
            await set_bot_state(interview_id, "waiting", db, mongo_connected)
            admitted = False
            admission_start = asyncio.get_event_loop().time()
            admission_timeout = 120  # 2 minutes max for admission

            for _ in range(600):
                await page.wait_for_timeout(1000)

                try:
                    leave_button = await page.locator('button[aria-label="Leave call"]').count()
                    lobby_wait = await page.locator('text="Waiting for host"').count()
                    lobby_wait_2 = await page.locator('text="Waiting for admission"').count()

                    if leave_button > 0 and lobby_wait == 0 and lobby_wait_2 == 0:
                        admitted = True
                        break

                    body = await page.evaluate("() => document.body.innerText.toLowerCase()")
                    if any(x in body for x in ["not found", "has ended", "denied", "removed from"]):
                        msg = "❌ Denied or removed or meeting ended during wait"
                        await push_log(msg)
                        await ctx.close()
                        return
                except Exception:
                    pass

                elapsed = asyncio.get_event_loop().time() - admission_start
                if elapsed > admission_timeout:
                    msg = f"❌ Not admitted after {admission_timeout}s — stuck in lobby"
                    await push_log(msg)

                    if mongo_connected:
                        await db.interviews.update_one(
                            {"interview_id": interview_id},
                            {"$set": {
                                "status": "abandoned",
                                "ended_at": datetime.utcnow(),
                                "abandon_reason": f"lobby_timeout ({admission_timeout}s)"
                            }}
                        )
                        msg = "✅ Marked as abandoned in MongoDB"
                        await push_log(msg)
                    await set_bot_state(interview_id, "abandoned", db, mongo_connected)
                    await ctx.close()
                    return

            msg = "✅ Admitted to meeting!"
            await push_log(msg)

            # ════════════════════════════════════════════════════════════
            # AUDIO ROUTING
            # ════════════════════════════════════════════════════════════
            # 1. Chrome WebRTC mic → virtual_mic_source (TTS audio)
            # 2. Web Speech API → BotMic (meeting audio from others)
            # ════════════════════════════════════════════════════════════

            msg = "⏳ Waiting 2 seconds for audio to stabilize..."
            await push_log(msg)
            await page.wait_for_timeout(2000)

            # Route Chrome output → VirtualSink, switch default source → BotMic,
            # verify Chrome mic source-output is on virtual_mic_source
            await setup_audio_routing_after_admission(page)

            # Inject Web Speech API → uses BotMic (new default source)
            msg = "🎤 Injecting Web Speech API..."
            await push_log(msg)
            await inject_speech_recognition(page, session_id, STT_LANGUAGE)
            msg = "✅ Web Speech API active (capturing meeting audio via BotMic)"
            await push_log(msg)

            # Short delay to let STT initialize
            await page.wait_for_timeout(500)

            # Set admitted — triggers greeting
            try:
                await set_bot_state(interview_id, "admitted", db, mongo_connected)
                await push_log("✅ bot_status=admitted → greeting will trigger")
            except Exception as _dbe:
                await push_log(f"⚠️  Failed to set bot_status: {_dbe}")

            # Start heartbeat
            async def _send_heartbeat():
                try:
                    while True:
                        await asyncio.sleep(10)
                        if mongo_connected:
                            await db.interviews.update_one(
                                {"interview_id": interview_id},
                                {"$set": {"bot_heartbeat": datetime.utcnow()}}
                            )
                        try:
                            from redis_client import get_redis, RedisState
                            redis = await get_redis()
                            state = RedisState(redis)
                            await state.set_bot_heartbeat(interview_id)
                        except Exception as redis_err:
                            msg = f"⚠️  Redis heartbeat error: {redis_err}"
                            print(msg)
                except Exception as e:
                    msg = f"⚠️  Heartbeat error: {e}"
                    await push_log(msg)

            heartbeat_task = asyncio.create_task(_send_heartbeat())

            # Periodic audio routing maintenance
            audio_routing_task = await create_audio_router()

            # STT health monitoring — verify recognition is active, auto-restart if stalled
            async def _monitor_stt_health():
                """
                Check every 10s that Web Speech API is running.
                After 3 consecutive not-running checks (30s stalled), force-restart
                by calling window.__stt_restart() from Python.
                """
                not_running_streak = 0
                try:
                    while True:
                        await asyncio.sleep(10)
                        try:
                            health = await page.evaluate("() => window.__stt_health()")
                            if health:
                                is_running = health.get('isRunning', False)
                                is_active  = health.get('interviewActive', True)
                                msg = (f"🎤 STT health: running={is_running}, "
                                       f"backoff={health.get('restartBackoff')}ms, "
                                       f"active={is_active}, "
                                       f"bot_speaking={health.get('isBotSpeaking')}")
                                await push_log(msg)

                                if not is_running and is_active:
                                    not_running_streak += 1
                                    if not_running_streak >= 3:
                                        # STT stalled for 30s — force restart
                                        msg = f"⚠️  STT stalled {not_running_streak * 10}s — force-restarting"
                                        await push_log(msg)
                                        try:
                                            await page.evaluate("window.__stt_restart()")
                                            not_running_streak = 0
                                        except Exception as restart_err:
                                            msg = f"⚠️  STT force-restart failed: {restart_err}"
                                            await push_log(msg)
                                else:
                                    not_running_streak = 0
                            else:
                                msg = "🎤 STT health: no data (page may be closed)"
                                await push_log(msg)
                        except Exception as stt_err:
                            msg = f"⚠️  STT health check failed: {stt_err}"
                            print(msg)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    msg = f"⚠️  STT monitor error: {e}"
                    await push_log(msg)

            stt_health_task = asyncio.create_task(_monitor_stt_health())

            async def _monitor_pulseaudio_health():
                """Check PulseAudio is alive every 30s. Attempt restart if it crashes."""
                try:
                    while True:
                        await asyncio.sleep(30)
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "pactl", "info",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            _, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                            if proc.returncode != 0:
                                raise RuntimeError(f"pactl info returned {proc.returncode}")
                        except Exception as pa_err:
                            msg = f"PulseAudio health check FAILED: {pa_err} — attempting restart"
                            await push_log(msg)
                            try:
                                from redis_client import get_redis, RedisState
                                redis = await get_redis()
                                state = RedisState(redis)
                                await state.set_bot_status(interview_id, "audio_error")
                            except Exception:
                                pass
                            try:
                                restart = await asyncio.create_subprocess_exec(
                                    "pulseaudio", "--start",
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                )
                                await asyncio.wait_for(restart.communicate(), timeout=10)
                                msg = "PulseAudio restart attempted"
                                await push_log(msg)
                            except Exception as restart_err:
                                msg = f"PulseAudio restart failed: {restart_err}"
                                await push_log(msg)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    msg = f"PulseAudio monitor error: {e}"
                    await push_log(msg)

            pa_health_task = asyncio.create_task(_monitor_pulseaudio_health())

            # Watch bot_speaking flag for echo cancellation
            speaking_watcher_task = await create_echo_guard_watcher(
                db, interview_id, status_state, page, mongo_connected
            )

            # State-sync heartbeat: periodically verify Python's bot_speaking
            # against MongoDB. A single dropped Redis "idle" pub/sub message
            # can leave status_state["bot_speaking"] = True permanently, causing
            # ALL future candidate speech to be treated as interrupt attempts —
            # the interview goes deaf with no error logged.
            async def _sync_bot_speaking_state():
                try:
                    while True:
                        await asyncio.sleep(10)
                        if not status_state.get("bot_speaking", False):
                            continue
                        try:
                            doc = await db.interviews.find_one(
                                {"interview_id": interview_id},
                                projection={"bot_speaking": 1}
                            )
                            if doc and not doc.get("bot_speaking", True):
                                await push_log(
                                    "[STATE SYNC] bot_speaking desync detected — "
                                    "Redis said speaking but MongoDB says idle. Force-syncing."
                                )
                                status_state["bot_speaking"] = False
                                try:
                                    await page.evaluate("window.__tts_ended(false)")
                                except Exception:
                                    pass
                        except Exception as sync_err:
                            await push_log(f"[STATE SYNC] Check failed (non-fatal): {sync_err}")
                except asyncio.CancelledError:
                    pass

            state_sync_task = asyncio.create_task(_sync_bot_speaking_state())

            # Inactivity timeout
            INACTIVITY_TIMEOUT = 600

            async def _check_inactivity():
                try:
                    while True:
                        await asyncio.sleep(60)
                        elapsed = asyncio.get_event_loop().time() - get_last_speech_time(interview_id)
                        if elapsed > INACTIVITY_TIMEOUT:
                            msg = f"⏰ Meeting TIMEOUT ({INACTIVITY_TIMEOUT}s) — no candidate speech"
                            await push_log(msg)
                            try:
                                await page.evaluate("window.__stop_interview()")
                            except Exception:
                                pass
                            if mongo_connected:
                                await db.interviews.update_one(
                                    {"interview_id": interview_id},
                                    {"$set": {
                                        "status": "completed",
                                        "ended_at": datetime.utcnow(),
                                        "abandon_reason": f"inactivity_timeout ({INACTIVITY_TIMEOUT}s)"
                                    }}
                                )
                            await set_bot_state(interview_id, "completed", db, mongo_connected)
                            if leave_task and not leave_task.done():
                                leave_task.cancel()
                            return
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    msg = f"⚠️  Inactivity checker error: {e}"
                    await push_log(msg)

            inactivity_task = asyncio.create_task(_check_inactivity())

            # Watch agent transcripts — flush any partial candidate buffer before agent speaks.
            # NOTE: does NOT call __tts_started() here. TTS arm() sets bot_speaking=True in
            # MongoDB, which _watch_bot_speaking picks up and calls __tts_started() at the
            # moment audio actually plays — not 3-4s early when the transcript is inserted.
            async def _watch_agent_transcripts():
                if not mongo_connected or db is None:
                    return

                pipeline = [
                    {
                        "$match": {
                            "operationType": "insert",
                            "fullDocument.speaker": "agent",
                            "fullDocument.interview_id": interview_id,
                        }
                    }
                ]

                retry_delay = 1.0
                while True:
                    try:
                        async with db.transcripts.watch(pipeline, full_document="updateLookup") as stream:
                            retry_delay = 1.0
                            async for change in stream:
                                try:
                                    if _redis:
                                        from stt_speech_buffer import _flush_speech_buffer, append_speech
                                        buf = await _redis.get(f"speech:{interview_id}:buffer")
                                        if buf and buf.decode().strip():
                                            msg = "🤖 Agent incoming — flushing partial candidate buffer"
                                            await push_log(msg)
                                            await _flush_speech_buffer(_redis, interview_id)
                                except Exception as inner_err:
                                    msg = f"⚠️ Agent transcript watcher inner error: {inner_err}"
                                    await push_log(msg)
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        msg = f"⚠️ Agent transcript watcher error (retry in {retry_delay}s): {e}"
                        await push_log(msg)
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 30.0)

            agent_transcript_task = asyncio.create_task(_watch_agent_transcripts())

            # Start meeting leave watcher
            leave_task = asyncio.create_task(
                watch_for_leave(interview_id, page, db, mongo_connected)
            )

            # Wait for meeting to end
            try:
                await leave_task
            except asyncio.CancelledError:
                pass

            # Cancel all background tasks
            for task in [heartbeat_task, audio_routing_task, stt_health_task,
                         pa_health_task, speaking_watcher_task, agent_transcript_task, inactivity_task,
                         state_sync_task]:
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            # Update status when meeting ends
            msg = "Meeting ended — Web Speech API transcription complete"
            await push_log(msg)

            if mongo_connected:
                try:
                    await db.interviews.update_one(
                        {"interview_id": interview_id},
                        {"$set": {
                            "status": "completed",
                            "ended_at": datetime.utcnow()
                        }}
                    )
                    msg = "✅ Marked as completed in MongoDB"
                    await push_log(msg)
                except Exception as e:
                    msg = f"⚠️  Failed to update status: {e}"
                    await push_log(msg)

        except Exception as e:
            msg = f"❌ Bot error: {e}"
            await push_log(msg)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            cancel_pending_flush()
            if mongo_connected:
                await disconnect_from_mongo()

            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


async def main():
    """Entry point for testing — watches for new interviews."""
    from main import watch_for_interviews
    await watch_for_interviews()


if __name__ == "__main__":
    asyncio.run(main())
