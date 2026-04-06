"""
bot.py — Google Meet Bot (Web Speech API Mode)
─────────────────────────────────────────────────────────────────────
Joins Google Meet as a guest and transcribes speech via Web Speech API.

BATCH PROCESSING:
- Captures ALL speech during meeting
- Buffers everything locally
- At meeting end: inserts ONE final transcript with complete conversation
- Agent then processes the entire conversation at once

NO real-time inserts. NO back-and-forth during meeting.
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
from mongo_handler import (
    connect_to_mongo,
    disconnect_from_mongo,
    insert_transcript,
)
import mongo_handler  # Import module to access db dynamically
from speech_injector import inject_speech_recognition

# Load environment variables (from Docker env or .env file)
load_dotenv()

# STT Language configuration (from .env)
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en-US")  # Default: English US

# Store captured transcripts locally during meeting (BATCH mode)
_transcript_buffer = []

# Buffer for current candidate speech (accumulates during speech session)
last_speech_time = asyncio.get_event_loop().time()
_speech_buffer = ""
_speech_timer = None
_last_saved_text = ""  # Track what we already saved to prevent duplicates
# 1.5s silence = speech ended. 0.3s was too aggressive — mid-sentence pauses
# split one response into multiple DB documents, causing the agent to reply to fragments.
_SPEECH_TIMEOUT_SEC = 1.5


async def _flush_speech_buffer(interview_id: str) -> None:
    """Save buffered speech to DB after candidate stops speaking."""
    global _speech_buffer, _speech_timer, _last_saved_text, last_speech_time

    if _speech_timer:
        _speech_timer.cancel()
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


async def _ensure_mic_on(page) -> None:
    """Ensure Google Meet's microphone is UNMUTED."""
    mic_off_selectors = [
        'button[aria-label="Turn on microphone"]',
        'button[aria-label="Unmute microphone"]',
    ]
    for attempt in range(4):
        for sel in mic_off_selectors:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    await page.wait_for_timeout(500)
                    msg = f"✅ Microphone unmuted (attempt {attempt+1})"
                    print(msg); await push_log(msg)
                    return
            except Exception:
                pass
        await page.wait_for_timeout(500)
    msg = "ℹ️  Microphone already on"
    print(msg); await push_log(msg)


async def _disable_camera(page) -> None:
    """Turn off the camera."""
    cam_selectors = [
        'button[aria-label="Turn off camera"]',
        'button[aria-label*="camera" i][aria-pressed="false"]',
    ]
    for attempt in range(5):
        for sel in cam_selectors:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    await page.wait_for_timeout(400)
                    msg = f"✅ Camera disabled (attempt {attempt + 1})"
                    print(msg); await push_log(msg)
                    return
            except Exception:
                pass
        await page.wait_for_timeout(500)
    msg = "ℹ️  Camera already off"
    print(msg); await push_log(msg)


async def _watch_for_leave(interview_id: str, page, db=None, mongo_connected=False) -> None:
    """
    Background task: polls interviews.interviews every 2s.
    When status becomes 'abandoned' or 'completed', exits IMMEDIATELY.

    Also includes a 2-hour timeout as fallback (matches MAX_INTERVIEW_DURATION_MINUTES).
    """
    start_time = asyncio.get_event_loop().time()
    timeout_seconds = 7200  # 2 hours fallback (matches MAX_INTERVIEW_DURATION_MINUTES)
    last_status_check = None

    while True:
        await asyncio.sleep(2)  # Poll every 2 seconds (balances responsiveness with DB load)

        # Check timeout
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > timeout_seconds:
            msg = f"⏰ Meeting TIMEOUT ({timeout_seconds}s) — leaving"
            print(msg); await push_log(msg)

            # Update status to abandoned so UI knows meeting ended
            try:
                if mongo_connected and db:
                    await db.interviews.update_one(
                        {"interview_id": interview_id},
                        {"$set": {"status": "abandoned", "ended_at": datetime.utcnow()}}
                    )
                    msg = "✅ Updated status=abandoned in MongoDB (timeout)"
                    print(msg); await push_log(msg)
            except Exception as e:
                msg = f"❌ Failed to update status: {e}"
                print(msg); await push_log(msg)

            # Force leave via Playwright
            try:
                for sel in ['button[aria-label="Leave call"]', 'button[aria-label*="Leave" i]']:
                    btn = page.locator(sel)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click()
                        await page.wait_for_timeout(500)
                        msg = "✅ Left meeting via Leave button (timeout)"
                        print(msg); await push_log(msg)
                        break
                await page.goto("about:blank", timeout=3000)
            except Exception as e:
                msg = f"⚠️  Force leave error: {e}"
                print(msg); await push_log(msg)
            return

        try:
            if not mongo_connected or db is None:
                continue

            interview = await db.interviews.find_one(
                {"interview_id": interview_id},
                projection={"status": 1}
            )
            
            current_status = interview.get("status") if interview else None
            
            # Log status changes
            if current_status != last_status_check:
                msg = f"📊 Status check: {current_status} (was: {last_status_check})"
                print(msg); await push_log(msg)
                last_status_check = current_status
            
            if interview and current_status in ("abandoned", "completed"):
                msg = f"🚨 Interview ENDED (status={current_status}) — leaving IMMEDIATELY"
                print(msg); await push_log(msg)

                # CRITICAL: Ensure status is set (in case UI set it but bot left before seeing it)
                try:
                    await db.interviews.update_one(
                        {"interview_id": interview_id},
                        {"$set": {"ended_at": datetime.utcnow()}}
                    )
                    msg = "✅ Confirmed status update in MongoDB"
                    print(msg); await push_log(msg)
                except Exception as e:
                    msg = f"⚠️  Status update error: {e}"
                    print(msg); await push_log(msg)

                # Force leave via Playwright IMMEDIATELY
                try:
                    for sel in ['button[aria-label="Leave call"]', 'button[aria-label*="Leave" i]']:
                        btn = page.locator(sel)
                        if await btn.count() > 0 and await btn.first.is_visible():
                            await btn.first.click()
                            await page.wait_for_timeout(500)
                            msg = "✅ Left meeting via Leave button (UI triggered)"
                            print(msg); await push_log(msg)
                            break
                    await page.goto("about:blank", timeout=3000)
                except Exception as e:
                    msg = f"⚠️  Leave error: {e}"
                    print(msg); await push_log(msg)
                return
        except Exception as e:
            msg = f"⚠️  Status check error: {e}"
            print(msg); await push_log(msg)
            pass


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
    global _transcript_buffer, _speech_buffer, _speech_timer, last_speech_time
    _transcript_buffer = []  # Reset buffer
    last_speech_time = asyncio.get_event_loop().time()  # reset inactivity clock for this session
    _speech_buffer = ""
    _speech_timer = None
    
    temp_dir = tempfile.mkdtemp(prefix="meet_bot_")
    session_id = interview_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    name = email.split("@")[0]

    msg = f"🤖 Bot: {name}  [WEB SPEECH API BATCH MODE]  session={session_id}"
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
    
    # Get the live db reference after connection
    db = mongo_handler.db
    
    if not mongo_connected:
        msg = "⚠️  Continuing without MongoDB (local transcripts only)"
        print(msg); await push_log(msg)

    async with async_playwright() as p:
        try:
            # FIX: Remove stale Chrome lock files (prevents "ProcessSingleton" errors)
            # Chrome creates 3 lock files: SingletonLock, SingletonCookie, SingletonSocket
            # These persist across container restarts and block new Chrome launches
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

            # Use persistent Chrome profile for cookies/login state
            # NOTE: headless=False so you can see the browser via VNC for debugging/admission
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=chrome_profile_dir,
                headless=False,  # Set to False so you can see the bot via VNC to admit it
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
                    # Keep profile clean
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--disable-sync",
                    "--no-first-run",
                    # Full screen
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

            # ── Console event handler — capture Web Speech API transcripts ─────
            # JavaScript handles buffering & silence detection (2s)
            # Python just saves what JavaScript flushes
            # Filter: Ignore bot's own greeting (prevents echo loop)
            GREETING_PHRASES = [
                "hello welcome to your interview",
                "could you please start by introducing yourself",
                "hello welcome",
                "start by introducing",
                "please start by introducing",
                "welcome to your interview",
                "introducing yourself",
                "could you please type",  # Variation seen in logs
                "hello welcome to your interview could you please",
            ]
            
            # Track bot speaking status for interruption and echo cancellation
            status_state = {"bot_speaking": False}
            
            async def _handle_console(msg):
                try:
                    text = msg.text
                    global _speech_buffer, _speech_timer

                    # Interim results — real-time partial transcript, log only.
                    # Interrupt is triggered by STT_SPEECH_START (once per utterance),
                    # not here — avoids flooding MongoDB on every word.
                    if text.startswith('STT_INTERIM:'):
                        interim_text = text.split('STT_INTERIM:', 1)[1].strip()
                        if interim_text:
                            msg = f"🎤 [INTERIM] {interim_text[:120]}"
                            print(msg)  # stdout only, not push_log (too noisy for remote logs)
                        return

                    # Capture speech start - reset buffer and inactivity clock
                    if text.startswith('STT_SPEECH_START:'):
                        global last_speech_time
                        last_speech_time = asyncio.get_event_loop().time()
                        _speech_buffer = ""
                        msg = "🎤 [SPEECH STARTED] Candidate speaking..."
                        print(msg); await push_log(msg)
                        # Trigger interrupt if bot is currently speaking (candidate cutting in)
                        if status_state["bot_speaking"] and mongo_connected and db is not None:
                            try:
                                await db.interviews.update_one(
                                    {"interview_id": interview_id},
                                    {"$set": {"tts_interrupt": True}}
                                )
                                msg_log = "⚡ [INTERRUPT] Candidate interrupted bot"
                                print(msg_log); await push_log(msg_log)
                            except Exception as int_err:
                                print(f"⚠️ Interrupt signal error: {int_err}")
                        return

                    # Capture speech end - schedule save after silence
                    if text.startswith('STT_SPEECH_END:'):
                        msg = f"🔇 [SPEECH ENDED] Finalizing transcript (timeout={_SPEECH_TIMEOUT_SEC}s)..."
                        print(msg); await push_log(msg)
                        # Cancel existing timer
                        if _speech_timer:
                            _speech_timer.cancel()
                        # Schedule save after a short delay to ensure all TRANSCRIPT_EVENTs are in
                        _speech_timer = asyncio.create_task(
                            asyncio.sleep(_SPEECH_TIMEOUT_SEC)
                        )
                        await _speech_timer
                        await _flush_speech_buffer(interview_id)
                        return

                    # Capture TRANSCRIPT_EVENT from Web Speech API (final results - accumulate during speech)
                    if text.startswith('TRANSCRIPT_EVENT:'):
                        final_text = text.split('TRANSCRIPT_EVENT:', 1)[1].strip()
                        if final_text:
                            # Accumulate into buffer (candidate may continue speaking)
                            if _speech_buffer:
                                _speech_buffer += " " + final_text
                            else:
                                _speech_buffer = final_text
                            
                            msg = f"🎤 [STT FINAL - BUFFERING] \"{final_text}\" → Buffer: \"{_speech_buffer[:100]}...\""
                            print(msg); await push_log(msg)
                        return

                    # Log other STT events for debugging
                    elif text.startswith('STT_') or '[STT]' in text or '[BOT]' in text:
                        # Only log important events
                        if "error" in text.lower() or "active" in text.lower() or "capture" in text.lower():
                            msg = f"🎤 {text[:200]}"
                            print(msg); await push_log(msg)
                        
                except Exception as e:
                    print(f"Console handler error: {e}")
            
            page.on('console', _handle_console)
            page.on('pageerror', lambda err: print(f"[PAGE ERROR] {err}"))

            # ── Inject init script: block devicechange ───
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
            await _ensure_mic_on(page)
            await _disable_camera(page)

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
            admitted = False
            admission_start = asyncio.get_event_loop().time()
            admission_timeout = 120  # 2 minutes max for admission
            
            for _ in range(600):
                await page.wait_for_timeout(1000)
                
                # Check for actual meeting UI (not just Leave button)
                try:
                    # Look for meeting UI elements that confirm we're actually in meeting
                    leave_button = await page.locator('button[aria-label="Leave call"]').count()
                    lobby_wait = await page.locator('text="Waiting for host"').count()
                    lobby_wait_2 = await page.locator('text="Waiting for admission"').count()
                    
                    # Actually admitted if: Leave button visible AND no lobby messages
                    if leave_button > 0 and lobby_wait == 0 and lobby_wait_2 == 0:
                        admitted = True
                        break
                        
                    # Check if meeting ended/denied
                    body = await page.evaluate("() => document.body.innerText.toLowerCase()")
                    if any(x in body for x in ["not found", "has ended", "denied"]):
                        msg = "❌ Denied or meeting ended during wait"
                        print(msg); await push_log(msg)
                        await ctx.close()
                        return
                except Exception:
                    pass
                
                # Check admission timeout
                elapsed = asyncio.get_event_loop().time() - admission_start
                if elapsed > admission_timeout:
                    msg = f"❌ Not admitted after {admission_timeout}s — stuck in lobby"
                    print(msg); await push_log(msg)

                    # Mark as abandoned so UI updates
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
                    
                    await ctx.close()
                    return

            msg = "✅ Admitted to meeting!"
            print(msg); await push_log(msg)

            # CRITICAL: Switch default source to BotMic for STT (matches other project)
            # This ensures Web Speech API captures meeting audio, not TTS silence
            try:
                result = await asyncio.create_subprocess_exec(
                    "pactl", "set-default-source", "BotMic",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await result.communicate()
                msg = f"✅ Switched default source to BotMic for STT"
                print(msg); await push_log(msg)
            except Exception as e:
                msg = f"⚠️  Failed to switch to BotMic: {e}"
                print(msg); await push_log(msg)

            # Signal interview-agent to send greeting (after proper delay)
            try:
                from motor.motor_asyncio import AsyncIOMotorClient as _MotorClient
                _int_uri = os.getenv("MONGODB_URI", "mongodb://mongodb:27017/?replicaSet=rs0")
                _int_db  = os.getenv("MONGODB_DB", "interviews")
                _ic = _MotorClient(_int_uri, serverSelectionTimeoutMS=5000)
                
                # Wait 2 seconds for bot to fully join meeting audio
                msg = "⏳ Waiting 2 seconds before greeting..."
                print(msg); await push_log(msg)
                await page.wait_for_timeout(2000)
                
                await _ic[_int_db]["interviews"].update_one(
                    {"interview_id": interview_id},
                    {"$set": {"bot_status": "admitted"}},
                )
                _ic.close()
                await push_log(f"✅ bot_status=admitted → greeting will trigger")
            except Exception as _dbe:
                await push_log(f"⚠️  Failed to set bot_status: {_dbe}")

            # Wait additional 3 seconds for audio to stabilize (total 5s)
            await page.wait_for_timeout(3000)

            # Inject Web Speech API
            msg = "🎤 Injecting Web Speech API for BATCH transcription..."
            print(msg); await push_log(msg)
            
            await inject_speech_recognition(page, session_id)
            
            msg = "✅ Web Speech API active — capturing speech (will save at end)"
            print(msg); await push_log(msg)

            # Start heartbeat (sends bot_heartbeat to DB and Redis every 10 seconds)
            async def _send_heartbeat():
                """Send heartbeat to MongoDB and Redis every 10 seconds."""
                try:
                    while True:
                        await asyncio.sleep(10)
                        if mongo_connected:
                            await db.interviews.update_one(
                                {"interview_id": interview_id},
                                {"$set": {"bot_heartbeat": datetime.utcnow()}}
                            )
                        # Send Redis heartbeat
                        try:
                            from redis_client import get_redis, RedisState
                            redis = await get_redis()
                            state = RedisState(redis)
                            await state.set_bot_heartbeat(interview_id)
                        except Exception as redis_err:
                            # Degraded mode without Redis
                            msg = f"⚠️  Redis heartbeat error: {redis_err}"
                            print(msg)
                except Exception as e:
                    msg = f"⚠️  Heartbeat error: {e}"
                    print(msg); await push_log(msg)

            heartbeat_task = asyncio.create_task(_send_heartbeat())

            # Watch bot_speaking flag (set by TTS interrupt_handler.arm/disarm) to
            # pause Web Speech API while agent is talking — prevents echo transcription.
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
                            retry_delay = 1.0  # reset on successful open
                            async for change in stream:
                                try:
                                    full_doc = change.get("fullDocument") or {}
                                    is_speaking = full_doc.get("bot_speaking", False)

                                    if is_speaking == status_state["bot_speaking"]:
                                        continue  # no change

                                    status_state["bot_speaking"] = is_speaking

                                    if is_speaking:
                                        msg = "🔇 Bot speaking — clearing buffer + gating STT"
                                        print(msg); await push_log(msg)
                                        global _speech_buffer, _last_saved_text
                                        _speech_buffer = ""
                                        _last_saved_text = ""
                                        try:
                                            await page.evaluate("window.__tts_started()")
                                        except Exception as eval_err:
                                            msg = f"⚠️ __tts_started eval error: {eval_err}"
                                            print(msg); await push_log(msg)
                                    else:
                                        msg = "🔊 Bot done speaking — resuming STT (echo gate active)"
                                        print(msg); await push_log(msg)
                                        try:
                                            await page.evaluate("window.__tts_ended()")
                                        except Exception as eval_err:
                                            msg = f"⚠️ __tts_ended eval error: {eval_err}"
                                            print(msg); await push_log(msg)
                                except Exception as inner_err:
                                    msg = f"⚠️ Bot speaking watcher inner error: {inner_err}"
                                    print(msg); await push_log(msg)
                    except asyncio.CancelledError:
                        return  # Normal shutdown — don't retry
                    except Exception as e:
                        msg = f"⚠️ Bot speaking watcher error (retry in {retry_delay}s): {e}"
                        print(msg); await push_log(msg)
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 30.0)

            speaking_watcher_task = asyncio.create_task(_watch_bot_speaking())

            # 10-minute inactivity timeout — resets every time candidate speaks.
            # last_speech_time is the module-level variable updated by _flush_speech_buffer
            # and STT_SPEECH_START. Using global ensures all paths share the same clock.
            INACTIVITY_TIMEOUT = 600  # 10 minutes

            async def _check_inactivity():
                """Leave meeting if no candidate speech for 10 minutes."""
                global last_speech_time
                try:
                    while True:
                        await asyncio.sleep(60)  # Check every minute
                        elapsed = asyncio.get_event_loop().time() - last_speech_time
                        if elapsed > INACTIVITY_TIMEOUT:
                            msg = f"⏰ Meeting TIMEOUT ({INACTIVITY_TIMEOUT}s) — no candidate speech"
                            print(msg); await push_log(msg)
                            # Mark as completed
                            if mongo_connected:
                                await db.interviews.update_one(
                                    {"interview_id": interview_id},
                                    {"$set": {
                                        "status": "completed",
                                        "ended_at": datetime.utcnow(),
                                        "abandon_reason": f"inactivity_timeout ({INACTIVITY_TIMEOUT}s)"
                                    }}
                                )
                            # Cancel leave task to trigger cleanup
                            if leave_task and not leave_task.done():
                                leave_task.cancel()
                            return
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    msg = f"⚠️  Inactivity checker error: {e}"
                    print(msg); await push_log(msg)

            inactivity_task = asyncio.create_task(_check_inactivity())

            # Bidirectional interruption: watch for incoming agent transcripts and
            # pre-gate STT immediately when the agent is ABOUT to speak — faster than
            # waiting for the bot_speaking change stream because it fires on the
            # transcript INSERT itself, before arm() + 400ms delay even starts.
            async def _watch_agent_transcripts():
                """Pre-gate STT the moment an agent transcript is inserted."""
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
                                    # Agent transcript just landed — flush any partial candidate
                                    # speech and close the STT gate immediately.
                                    if _speech_buffer.strip():
                                        msg = "🤖 Agent incoming — flushing partial candidate buffer"
                                        print(msg); await push_log(msg)
                                        await _flush_speech_buffer(interview_id)

                                    # Pre-gate STT so we don't capture the start of TTS audio
                                    if not status_state["bot_speaking"]:
                                        status_state["bot_speaking"] = True
                                        try:
                                            await page.evaluate("window.__tts_started()")
                                            msg = "🤖 [PRE-GATE] STT closed — agent transcript incoming"
                                            print(msg); await push_log(msg)
                                        except Exception as eval_err:
                                            msg = f"⚠️ Pre-gate eval error: {eval_err}"
                                            print(msg); await push_log(msg)
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

            # Run meeting monitor and leave watcher concurrently
            leave_task = asyncio.create_task(
                _watch_for_leave(interview_id, page, db, mongo_connected)
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

            # CRITICAL: Update status when meeting ends naturally
            msg = "Meeting ended — Web Speech API transcription complete"
            print(msg); await push_log(msg)

            # Mark interview as completed (bot finished normally)
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

            # Final cleanup - save any remaining buffered speech
            if _speech_buffer.strip():
                await _flush_speech_buffer(interview_id)

            # Final cleanup - save batch transcript
            if mongo_connected and _transcript_buffer:
                full_text = "\n".join(_transcript_buffer)
                await insert_transcript(interview_id, "candidate", full_text)
                msg = f"✅ Final transcript saved: {len(_transcript_buffer)} utterances, {len(full_text)} chars"
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
            
            # Cleanup temp dir
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


async def main():
    """Main entry point — watches interviews.interviews for new meetings."""
    from motor.motor_asyncio import AsyncIOMotorClient

    mongo_uri = os.getenv("MONGODB_URI", "mongodb://host.docker.internal:27017/?replicaSet=rs0")
    db_name = os.getenv("MONGODB_DB", "interviews")
    bot_email = os.getenv("BOT_EMAIL", "bot@example.com")
    
    print(f"[WATCHER] Connecting to MongoDB: {mongo_uri}")
    client = AsyncIOMotorClient(mongo_uri)
    
    # Wait for MongoDB
    for attempt in range(30):
        try:
            await client.admin.command("ping")
            print("[WATCHER] ✅ MongoDB connected")
            break
        except Exception as e:
            print(f"[WATCHER] Waiting for MongoDB ({attempt + 1}/30): {e}")
            await asyncio.sleep(3)
    else:
        print("[WATCHER] ❌ Could not connect to MongoDB after 90s — exiting")
        return
    
    db = client[db_name]
    collection = db["interviews"]
    
    pipeline = [{"$match": {"operationType": "insert"}}]
    
    print(f"[WATCHER] 👀 Watching interviews.interviews for new sessions...")
    print(f"[WATCHER] Bot email: {bot_email}")
    
    async with collection.watch(pipeline, full_document="updateLookup") as stream:
        async for change in stream:
            full_doc = change.get("fullDocument") or {}
            status = full_doc.get("status", "")
            
            if status != "in_progress":
                continue
            
            interview_id = full_doc.get("interview_id", "unknown")
            meeting_url = full_doc.get("meeting_url", "")
            
            if not meeting_url:
                print(f"[WATCHER] ⚠️  interview {interview_id} has no meeting_url — skipping")
                continue
            
            print(f"[WATCHER] 🎯 Joining meeting for interview {interview_id}: {meeting_url}")
            
            # Dispatch to background task
            asyncio.create_task(join_meeting_and_transcribe(
                url=meeting_url,
                email=bot_email,
                interview_id=interview_id,
            ))


if __name__ == "__main__":
    asyncio.run(main())
