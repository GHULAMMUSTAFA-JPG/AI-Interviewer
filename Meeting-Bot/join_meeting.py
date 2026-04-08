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
from speech_injector import inject_speech_recognition
from media_controls import ensure_mic_on, disable_camera
from state_manager import set_bot_state
from meeting_watcher import watch_for_leave
from speech_buffer import (
    init_speech_state, _flush_speech_buffer, get_speech_buffer,
    clear_speech_buffer, get_last_speech_time
)
from console_handler import create_console_handler

# Load environment variables
load_dotenv()
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en-US")


async def _route_all_sink_inputs_to_virtualsink() -> int:
    """
    Move Chrome's PulseAudio sink inputs to VirtualSink.

    Skips pacat sink inputs — those are TTS audio that must stay on
    virtual_mic so they flow: virtual_mic → virtual_mic.monitor →
    Chrome WebRTC mic input → meeting participants can hear the bot.

    Only Chrome's WebRTC output (participant voices) should go to
    VirtualSink so STT can read from VirtualSink.monitor.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "list", "sink-inputs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()

        # Parse full sink-inputs output: extract id and application.name
        current_id = None
        current_app = None
        sink_inputs = []  # list of (id, app_name)

        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Sink Input #"):
                if current_id is not None:
                    sink_inputs.append((current_id, current_app or ""))
                current_id = line.split("#")[1].strip()
                current_app = None
            elif "application.name" in line and "=" in line:
                # Format: application.name = "pacat" or "Chromium input"
                current_app = line.split("=", 1)[1].strip().strip('"')

        if current_id is not None:
            sink_inputs.append((current_id, current_app or ""))

        moved = 0
        for sink_input_id, app_name in sink_inputs:
            # Skip pacat — that's TTS audio playing on virtual_mic.
            # Moving it to VirtualSink would send bot speech to STT instead of
            # to Chrome's mic, so meeting participants would never hear the bot.
            if "pacat" in app_name.lower():
                continue

            mv = await asyncio.create_subprocess_exec(
                "pactl", "move-sink-input", sink_input_id, "VirtualSink",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await mv.communicate()
            if mv.returncode == 0:
                moved += 1

        return moved
    except Exception as e:
        print(f"⚠️  _route_all_sink_inputs error: {e}")
        return 0


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
    init_speech_state()  # Reset speech state
    clear_speech_buffer()

    temp_dir = tempfile.mkdtemp(prefix="meet_bot_")
    session_id = interview_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    name = email.split("@")[0]

    msg = f"🤖 Bot: {name}  [WEB SPEECH API]  lang={STT_LANGUAGE}  session={session_id}"
    print(msg); await push_log(msg)

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
        print(msg); await push_log(msg)

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
                        print(msg); await push_log(msg)
                    except Exception as e:
                        msg = f"⚠️  Failed to remove {lock_name}: {e} (will try anyway)"
                        print(msg); await push_log(msg)

            msg = "🌐 Launching Chromium (with persistent profile)..."
            print(msg); await push_log(msg)

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
            print(msg); await push_log(msg)

            await ctx.grant_permissions(["microphone"], origin="https://meet.google.com")

            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            # Track bot speaking status for interruption and echo cancellation
            status_state = {"bot_speaking": False}

            # Create console handler for STT events
            async def flush_callback(int_id):
                return await _flush_speech_buffer(int_id)

            await create_console_handler(
                page, interview_id, status_state, db, mongo_connected, flush_callback
            )
            page.on('pageerror', lambda err: print(f"[PAGE ERROR] {err}"))

            async def _on_page_navigated(frame):
                """Detect when Chrome navigates away from Google Meet — host removed bot."""
                if frame != page.main_frame:
                    return
                url = page.url
                if "meet.google.com" not in url and url not in ("about:blank", ""):
                    msg = f"🚨 Page navigated away from Meet → {url} — bot was removed"
                    print(msg); await push_log(msg)
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
            print(msg); await push_log(msg)
            try:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                msg = f"❌ Navigation failed: {e}"
                print(msg); await push_log(msg)
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
                        print(msg); await push_log(msg)
                        break
                except Exception:
                    pass

            # Pre-join: ensure mic ON + disable camera
            await ensure_mic_on(page)
            await disable_camera(page)

            # Click join button
            msg = "🔍 Clicking join button..."
            print(msg); await push_log(msg)
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
                print(msg); await push_log(msg)
                await ctx.close()
                return

            # Wait for admittance
            msg = "⏳ Waiting for host to admit (up to 10 min)..."
            print(msg); await push_log(msg)
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
                        print(msg); await push_log(msg)
                        await ctx.close()
                        return
                except Exception:
                    pass

                elapsed = asyncio.get_event_loop().time() - admission_start
                if elapsed > admission_timeout:
                    msg = f"❌ Not admitted after {admission_timeout}s — stuck in lobby"
                    print(msg); await push_log(msg)

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
                        print(msg); await push_log(msg)
                    await set_bot_state(interview_id, "abandoned", db, mongo_connected)
                    await ctx.close()
                    return

            msg = "✅ Admitted to meeting!"
            print(msg); await push_log(msg)

            # ════════════════════════════════════════════════════════════
            # AUDIO ROUTING — THE FINAL CORRECT SETUP
            # ════════════════════════════════════════════════════════════
            #
            # Goal 1: Chrome WebRTC mic reads TTS audio (virtual_mic_source)
            #         → Candidate hears the bot in the meeting
            #
            # Goal 2: Web Speech API reads meeting audio (BotMic)
            #         → STT captures other participants' speech
            #
            # How:
            #   1. Default source = virtual_mic_source (set by entrypoint.sh)
            #   2. Chrome starts → opens mic on virtual_mic_source
            #   3. After admission: route Chrome output → VirtualSink
            #   4. Switch default source → BotMic (only affects NEW streams)
            #   5. Verify Chrome mic is STILL on virtual_mic_source
            #   6. Inject Web Speech API → uses BotMic (new default)
            # ════════════════════════════════════════════════════════════

            msg = "⏳ Waiting 2 seconds for audio to stabilize..."
            print(msg); await push_log(msg)
            await page.wait_for_timeout(2000)

            # Step 1: Route Chrome WebRTC output → VirtualSink (meeting audio)
            n = await _route_all_sink_inputs_to_virtualsink()
            msg = f"🔊 Audio routing: moved {n} sink input(s) to VirtualSink"
            print(msg); await push_log(msg)

            # Step 2: Switch default source → BotMic
            # This ONLY affects NEW streams (like Web Speech API).
            # Chrome's existing WebRTC mic stream stays on virtual_mic_source
            # (verified in step 3).
            try:
                result = await asyncio.create_subprocess_exec(
                    "pactl", "set-default-source", "BotMic",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await result.communicate()
                msg = f"✅ Switched default source to BotMic (for STT only)"
                print(msg); await push_log(msg)
            except Exception as e:
                msg = f"⚠️  Failed to switch to BotMic: {e}"
                print(msg); await push_log(msg)

            # Step 3: Verify Chrome's mic source-output is on virtual_mic_source
            # If switching default source moved Chrome's stream, force it back.
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pactl", "list", "short", "source-outputs",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                lines = stdout.decode().strip().splitlines()
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 3:
                        source_output_id = parts[0]
                        source_idx = parts[2]
                        # VirtualSink.monitor = 0, virtual_mic.monitor = 2
                        # virtual_mic_source = 3 (index in sources list)
                        # We need Chrome on source index 3 (virtual_mic_source)
                        # or on source index 2 (virtual_mic.monitor)
                        if source_idx not in ("2", "3"):
                            msg = f"🔧 Chrome source-output #{source_output_id} on wrong source ({source_idx}), moving to virtual_mic_source (3)"
                            print(msg); await push_log(msg)
                            mv = await asyncio.create_subprocess_exec(
                                "pactl", "move-source-output", source_output_id, "3",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            await mv.communicate()
                            msg = f"✅ Moved Chrome source-output to virtual_mic_source"
                            print(msg); await push_log(msg)
            except Exception as e:
                msg = f"⚠️  Audio routing verification skipped: {e}"
                print(msg)

            # Step 4: Inject Web Speech API → uses BotMic (new default source)
            msg = "🎤 Injecting Web Speech API..."
            print(msg); await push_log(msg)
            await inject_speech_recognition(page, session_id, STT_LANGUAGE)
            msg = "✅ Web Speech API active (capturing meeting audio via BotMic)"
            print(msg); await push_log(msg)

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
                    print(msg); await push_log(msg)

            heartbeat_task = asyncio.create_task(_send_heartbeat())

            # FIX: Periodic audio routing maintenance — Chrome creates new sink-inputs
            # and source-outputs dynamically. Without periodic verification,
            # Chrome's mic might drift to the wrong source.
            async def _maintain_audio_routing():
                """Verify and fix audio routing every 5 seconds."""
                try:
                    while True:
                        await asyncio.sleep(5)
                        # Re-route sink-inputs (Chrome output → VirtualSink)
                        n = await _route_all_sink_inputs_to_virtualsink()
                        if n > 0:
                            msg = f"🔊 Audio routing: re-routed {n} sink-input(s) to VirtualSink"
                            print(msg); await push_log(msg)

                        # Verify source-outputs (Chrome mic → virtual_mic_source)
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                "pactl", "list", "short", "source-outputs",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            stdout, _ = await proc.communicate()
                            lines = stdout.decode().strip().splitlines()
                            for line in lines:
                                parts = line.split()
                                if len(parts) >= 3:
                                    source_output_id = parts[0]
                                    source_idx = parts[2]
                                    if source_idx not in ("2", "3"):
                                        msg = f"🔧 Chrome source-output #{source_output_id} drifted to source {source_idx}, fixing"
                                        print(msg); await push_log(msg)
                                        mv = await asyncio.create_subprocess_exec(
                                            "pactl", "move-source-output", source_output_id, "3",
                                            stdout=asyncio.subprocess.PIPE,
                                            stderr=asyncio.subprocess.PIPE,
                                        )
                                        await mv.communicate()
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    msg = f"⚠️  Audio routing maintenance error: {e}"
                    print(msg); await push_log(msg)

            audio_routing_task = asyncio.create_task(_maintain_audio_routing())

            # STT health monitoring — verify recognition is actually active
            async def _monitor_stt_health():
                """Check every 10s that Web Speech API is still functioning."""
                try:
                    while True:
                        await asyncio.sleep(10)
                        try:
                            health = await page.evaluate("() => window.__stt_health()")
                            if health:
                                msg = (f"🎤 STT health: running={health.get('isRunning')}, "
                                       f"backoff={health.get('restartBackoff')}ms, "
                                       f"active={health.get('interviewActive')}, "
                                       f"bot_speaking={health.get('isBotSpeaking')}")
                            else:
                                msg = f"🎤 STT health: no data (page may be closed)"
                            print(msg); await push_log(msg)
                        except Exception as stt_err:
                            msg = f"⚠️  STT health check failed: {stt_err}"
                            print(msg)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    msg = f"⚠️  STT monitor error: {e}"
                    print(msg); await push_log(msg)

            stt_health_task = asyncio.create_task(_monitor_stt_health())

            # Watch bot_speaking flag for echo cancellation
            async def _watch_bot_speaking():
                if not mongo_connected or db is None:
                    msg = "⚠️ Bot speaking watcher skipped — MongoDB not connected"
                    print(msg); await push_log(msg)
                    return

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
                        msg = "👁️ Bot speaking watcher: change stream opened"
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
                                        msg = "🔇 Bot speaking — clearing buffer + gating STT"
                                        print(msg); await push_log(msg)
                                        clear_speech_buffer()
                                        try:
                                            await page.evaluate("window.__tts_started()")
                                        except Exception as eval_err:
                                            msg = f"⚠️ __tts_started eval error: {eval_err}"
                                            print(msg); await push_log(msg)
                                    else:
                                        was_interrupted = bool(full_doc.get("tts_interrupt", False))
                                        gate_label = "no echo gate (interrupted)" if was_interrupted else "echo gate active"
                                        msg = f"🔊 Bot done speaking — resuming STT ({gate_label})"
                                        print(msg); await push_log(msg)
                                        try:
                                            js_flag = "true" if was_interrupted else "false"
                                            await page.evaluate(f"window.__tts_ended({js_flag})")
                                        except Exception as eval_err:
                                            msg = f"⚠️ __tts_ended eval error: {eval_err}"
                                            print(msg); await push_log(msg)
                                except Exception as inner_err:
                                    msg = f"⚠️ Bot speaking watcher inner error: {inner_err}"
                                    print(msg); await push_log(msg)
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        msg = f"⚠️ Bot speaking watcher error (retry in {retry_delay}s): {e}"
                        print(msg); await push_log(msg)
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 30.0)

            speaking_watcher_task = asyncio.create_task(_watch_bot_speaking())

            # Inactivity timeout
            INACTIVITY_TIMEOUT = 600

            async def _check_inactivity():
                try:
                    while True:
                        await asyncio.sleep(60)
                        elapsed = asyncio.get_event_loop().time() - get_last_speech_time()
                        if elapsed > INACTIVITY_TIMEOUT:
                            msg = f"⏰ Meeting TIMEOUT ({INACTIVITY_TIMEOUT}s) — no candidate speech"
                            print(msg); await push_log(msg)
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
                    print(msg); await push_log(msg)

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
                                    if get_speech_buffer().strip():
                                        msg = "🤖 Agent incoming — flushing partial candidate buffer"
                                        print(msg); await push_log(msg)
                                        await _flush_speech_buffer(interview_id)
                                except Exception as inner_err:
                                    msg = f"⚠️ Agent transcript watcher inner error: {inner_err}"
                                    print(msg); await push_log(msg)
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        msg = f"⚠️ Agent transcript watcher error (retry in {retry_delay}s): {e}"
                        print(msg); await push_log(msg)
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
            for task in [heartbeat_task, speaking_watcher_task, agent_transcript_task, inactivity_task]:
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            # Update status when meeting ends
            msg = "Meeting ended — Web Speech API transcription complete"
            print(msg); await push_log(msg)

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
                    print(msg); await push_log(msg)
                except Exception as e:
                    msg = f"⚠️  Failed to update status: {e}"
                    print(msg); await push_log(msg)

        except Exception as e:
            msg = f"❌ Bot error: {e}"
            print(msg); await push_log(msg)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
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
