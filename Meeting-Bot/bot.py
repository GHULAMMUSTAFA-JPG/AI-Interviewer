"""
Meeting-Bot: joins a Google Meet and scrapes captions into interviews.transcripts.

Key differences from STT/bot_logic.py:
  - Accepts interview_id — all DB writes are keyed to a specific interview.
  - Filters out bot's own speech: any speaker name containing "(AI)" is skipped.
  - Writes ONLY to interviews.transcripts with speaker="candidate".
  - No summary / meeting metadata write (not needed here).
"""

import asyncio
import re
from pathlib import Path
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

from caption_scraper import scrape_meeting_captions
from logger import push_log
import mongo_handler
from mongo_handler import (
    connect_to_mongo,
    disconnect_from_mongo,
    insert_transcript,
)


async def _watch_for_leave(interview_id: str, page) -> None:
    """
    Background task: polls interviews.interviews every 1 s. When status becomes
    'abandoned' or 'completed', clicks the Leave call button and navigates away
    as a guaranteed fallback — which cancels the sibling tasks (caption scraper
    + audio watcher).
    """
    while True:
        await asyncio.sleep(1)
        try:
            db = mongo_handler.get_db()
            if db is None:
                continue

            interview = await db.interviews.find_one(
                {"interview_id": interview_id},
                projection={"status": 1}
            )
            if interview and interview.get("status") in ("abandoned", "completed"):
                msg = f"Interview {interview_id} ended — leaving meeting"
                print(msg); await push_log(msg)

                for sel in [
                    'button[aria-label="Leave call"]',
                    'button[aria-label*="Leave" i]',
                ]:
                    try:
                        btn = page.locator(sel)
                        if await btn.count() > 0 and await btn.first.is_visible():
                            await btn.first.click()
                            await page.wait_for_timeout(1000)
                            break
                    except Exception:
                        pass

                await page.goto("about:blank", timeout=5000)
                return
        except Exception:
            pass


async def _ensure_mic_on(page) -> None:
    """
    Ensure Google Meet's microphone is UNMUTED.

    Meet often defaults new joiners to mic-off.  If the mic is off, Chrome's
    WebRTC track has enabled=false → TTS audio never reaches participants.
    We click "Turn on microphone" (or equivalent) if it is visible.
    """
    mic_off_selectors = [
        'button[aria-label="Turn on microphone"]',
        'button[aria-label="Unmute microphone"]',
        'button[aria-label*="microphone" i][aria-pressed="true"]',
        'button[data-tooltip*="Turn on microphone" i]',
    ]
    for attempt in range(4):
        for sel in mic_off_selectors:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    await page.wait_for_timeout(500)
                    msg = f"   ✅ Microphone unmuted (clicked: {sel} attempt {attempt+1})"
                    print(msg); await push_log(msg)
                    return
            except Exception:
                pass
        await page.wait_for_timeout(500)
    msg = "   ℹ️  Microphone already on or mute button not found"
    print(msg); await push_log(msg)


async def _disable_camera(page) -> None:
    """
    Turn off the camera ONLY — microphone must stay ON.

    The bot's mic track in Meet carries TTS audio (virtual_mic_source → WebRTC).
    Muting Chrome's mic would silence the agent's voice in the meeting.
    """
    cam_selectors = [
        'button[aria-label="Turn off camera"]',
        'button[aria-label*="camera" i][aria-pressed="false"]',
        'button[aria-label*="Stop video" i]',
    ]

    async def _click_first_visible(selectors: list, label: str) -> None:
        for attempt in range(5):
            for sel in selectors:
                try:
                    btn = page.locator(sel)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click()
                        await page.wait_for_timeout(400)
                        msg = f"   ✅ {label} disabled (attempt {attempt + 1})"
                        print(msg); await push_log(msg)
                        return
                except Exception:
                    pass
            await page.wait_for_timeout(500)
        msg = f"   ℹ️  {label} already off or control not found"
        print(msg); await push_log(msg)

    await _click_first_visible(cam_selectors, "Camera")


async def join_meeting_and_transcribe(
    url: str,
    email: str,
    password: str = "",
    headless: bool = False,
    interview_id: str = None,
) -> None:
    """
    Join a Google Meet as guest and scrape captions into MongoDB.

    Flow:
        1. Launch Chromium with automation-friendly flags
        2. Navigate to meeting URL
        3. Enter guest name — bot appears as "<name> (AI)" so caption filter works
        4. Pre-join: disable mic + camera
        5. Click 'Join' / 'Ask to join'
        6. Wait for host to admit (up to 10 min)
        7. Post-admit: ensure mic ON (for TTS), confirm camera OFF
        8. Set bot_status=admitted → triggers Main-Agent to insert greeting
        9. Start caption scraper + leave watcher concurrently
        10. On exit: flush remaining buffers, mark interview abandoned if needed
    """
    import tempfile
    import shutil

    temp_dir = tempfile.mkdtemp(prefix="meet_bot_")
    session_id = interview_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    name = email.split("@")[0]

    msg = f"🤖 Bot: {name} (AI)  session={session_id}"
    print(msg); await push_log(msg)

    # Pre-grant microphone permission for meet.google.com in Chrome's profile.
    # This avoids the Web Speech API 'not-allowed' error that occurs even with
    # --use-fake-ui-for-media-stream when using a fresh persistent context.
    import json as _json
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
    (_prefs_dir / "Preferences").write_text(_json.dumps(_prefs))

    transcript_lines: list[str] = []
    mongo_connected = await connect_to_mongo()
    if not mongo_connected:
        msg = "⚠️  Continuing without MongoDB (local transcripts only)"
        print(msg); await push_log(msg)

    async with async_playwright() as p:
        try:
            msg = "🌐 Launching Chromium..."
            print(msg); await push_log(msg)

            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=headless,
                channel="chrome",        # use system Google Chrome (present in base image)
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--autoplay-policy=no-user-gesture-required",
                    "--use-fake-ui-for-media-stream",           # auto-grant mic/cam without popup
                    "--allow-running-insecure-content",         # allow http:// fetch from https:// page (backend POST)
                    "--disable-features=BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights",  # backend is private IP
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-position=0,0",
                    # Force audio through PulseAudio
                    "--disable-features=AudioServiceSandbox,AudioServiceOutOfProcess",
                    "--alsa-output-device=pulse",
                    "--alsa-input-device=pulse",
                    "--enable-speech-dispatcher",
                ],
                accept_downloads=False,
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 720},
            )

            # Grant microphone permission so Web Speech API is not blocked
            await ctx.grant_permissions(["microphone"], origin="https://meet.google.com")

            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            # ── Inject init script: block devicechange before Meet JS runs ───
            # Google Meet listens to navigator.mediaDevices 'devicechange' and calls
            # replaceTrack() when the PulseAudio default source changes.
            # When we later call `pactl set-default-source BotMic` (for STT), Chrome
            # fires devicechange in all pages → Meet renegotiates → moves its outgoing
            # WebRTC mic track from virtual_mic_source (TTS) to BotMic → TTS silent.
            # Blocking devicechange registration here prevents that renegotiation while
            # still allowing Chrome's SpeechRecognition audio service (separate process)
            # to honour the pactl default switch and capture BotMic.
            await page.add_init_script("""
(function blockDeviceChange() {
    var _origAdd = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function(type, listener, options) {
        if (this === navigator.mediaDevices && type === 'devicechange') {
            console.log('[BOT-INIT] devicechange listener BLOCKED (TTS routing protected)');
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
    console.log('[BOT-INIT] Audio device-change protection active — Meet WebRTC will stay on virtual_mic_source');
})();
""")
            await push_log("✅ [INIT] devicechange protection injected (Meet WebRTC pinned to virtual_mic_source)")

            # ── Navigate ───────────────────────────────────────────
            msg = f"🌐 Navigating → {url}"
            print(msg); await push_log(msg)
            nav_ok = False
            for attempt in range(1, 4):
                try:
                    await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                    nav_ok = True
                    break
                except Exception as e:
                    msg = f"Navigation attempt {attempt}/3 failed: {e}"
                    print(msg); await push_log(msg)
                    if attempt < 3:
                        await page.wait_for_timeout(3000)
            if not nav_ok:
                msg = "Navigation failed after 3 attempts — aborting"
                print(msg); await push_log(msg)
                await _abort("navigation failed")
                await ctx.close()
                return

            await page.wait_for_timeout(5000)

            # ── Enter guest name ───────────────────────────────────
            for selector in [
                "input[placeholder='Your name']",
                "input[aria-label='Your name']",
                "input[type='text']",
            ]:
                try:
                    if await page.locator(selector).count() > 0:
                        await page.locator(selector).first.fill(f"{name} (AI)")
                        await page.wait_for_timeout(1000)
                        msg = "Name entered"
                        print(msg); await push_log(msg)
                        break
                except Exception:
                    pass

            # NOTE: Microphone is intentionally left ON.
            # TTS plays audio to PulseAudio virtual_mic (null-sink).
            # Chrome reads from virtual_mic_source (the monitor) as its mic input.
            # Muting Chrome's mic would silence the agent's voice in the meeting.

            # Pre-join: turn off camera
            try:
                for cam_sel in [
                    'button[aria-label="Turn off camera"]',
                    'button[aria-label*="camera" i][aria-pressed="false"]',
                    'button[aria-label*="Stop video" i]',
                ]:
                    cam_btn = page.locator(cam_sel)
                    if await cam_btn.count() > 0 and await cam_btn.first.is_visible():
                        await cam_btn.first.click()
                        await page.wait_for_timeout(500)
                        break
            except Exception:
                pass

            # Click join button
            msg = "Joining meeting..."
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
                msg = "No join button found — aborting"
                print(msg); await push_log(msg)
                await _abort("no join button")
                await ctx.close()
                return

            # Wait for admittance
            msg = "Waiting for host to admit..."
            print(msg); await push_log(msg)
            admitted = False
            for _ in range(600):
                await page.wait_for_timeout(1000)
                try:
                    if await page.locator('button[aria-label="Leave call"]').count() > 0:
                        admitted = True
                        break
                    body = await page.evaluate("() => document.body.innerText.toLowerCase()")
                    if any(x in body for x in ["not found", "has ended", "denied"]):
                        msg = "Denied or meeting ended during wait"
                        print(msg); await push_log(msg)
                        await _abort("denied during wait")
                        await ctx.close()
                        return
                except Exception:
                    pass

            if not admitted:
                msg = "Not admitted within 10 minutes — aborting"
                print(msg); await push_log(msg)
                await _abort("10 min timeout")
                await ctx.close()
                return

            msg = "Admitted to meeting!"
            print(msg); await push_log(msg)

            # Post-admit: ensure mic ON (for TTS), confirm camera OFF
            await _ensure_mic_on(page)
            await _disable_camera(page)

            # ── Signal interview-agent to send greeting ────────────
            # _watch_new_interviews in main.py watches for this update.
            # Without it the greeting NEVER fires because the watcher
            # sits on "operationType=update, bot_status=admitted" forever.
            try:
                from motor.motor_asyncio import AsyncIOMotorClient as _MotorClient
                _int_uri = os.getenv("MONGODB_URI", "mongodb://mongodb:27017/?replicaSet=rs0")
                _int_db  = os.getenv("MONGODB_DB", "interviews")
                _ic = _MotorClient(_int_uri, serverSelectionTimeoutMS=5000)
                await _ic[_int_db]["interviews"].update_one(
                    {"interview_id": interview_id},
                    {"$set": {"bot_status": "admitted"}},
                )
                _ic.close()
                await push_log(f"✅ [BOT] bot_status=admitted → greeting will trigger")
            except Exception as _dbe:
                await push_log(f"⚠️  [BOT] Failed to set bot_status: {_dbe}")

            await page.wait_for_timeout(1500)

            # Run caption scraper and leave watcher concurrently.
            # Whichever finishes first (meeting ends or leave signal) cancels the other.
            scrape_task = asyncio.create_task(
                scrape_meeting_captions(page, session_id, on_caption, _on_speech_start)
            )
            leave_task = asyncio.create_task(
                _watch_for_leave(interview_id, page)
            )

            done, pending = await asyncio.wait(
                [scrape_task, leave_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(t), timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            msg = "Meeting ended — caption scraping complete"
            print(msg); await push_log(msg)

            # ── Final flush ───────────────────────────────────────────────
            # The meeting ended while some utterances may still be buffered
            # (timer not fired yet). Cancel pending timers and save immediately.
            for speaker, buf in _utterance_buffers.items():
                if buf["timer"] and not buf["timer"].done():
                    buf["timer"].cancel()
                remaining = " ".join(buf["parts"]).strip()
                if remaining and mongo_connected:
                    await insert_transcript(interview_id, "candidate", remaining)
                    msg = f"Final flush [{speaker}]: {remaining[:80]}"
                    print(msg); await push_log(msg)

            # ── Mark interview as abandoned if still in_progress ──────────
            # Covers the case where the bot was kicked / meeting ended
            # externally without going through the UI's Leave button.
            db = mongo_handler.get_db()
            if db is not None:
                current = await db.interviews.find_one(
                    {"interview_id": interview_id},
                    projection={"status": 1}
                )
                if current and current.get("status") == "in_progress":
                    await db.interviews.update_one(
                        {"interview_id": interview_id},
                        {"$set": {"status": "abandoned", "ended_at": datetime.utcnow()}}
                    )
                    msg = "Interview marked abandoned — bot left meeting"
                    print(msg); await push_log(msg)

        except Exception as e:
            msg = f"Bot error: {e}"
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


# ── Utterance buffer ──────────────────────────────────────────────────
# The caption scraper fires on_caption once per *stabilized chunk*
# (~1.5 s of DOM silence), not once per full turn. A candidate
# saying three sentences would produce three DB inserts, causing
# Main-Agent to respond mid-thought three times.
#
# Fix: buffer chunks per speaker. Start a 4-second silence timer
# on each new chunk. If more text arrives, cancel and restart the
# timer. When the timer actually fires (real silence), flush the
# entire accumulated turn as ONE DB insert.
# ─────────────────────────────────────────────────────────────────────
UTTERANCE_SILENCE_SEC = 0.8   # wait for long/mid responses (was 1.2)
SHORT_UTTERANCE_SEC   = 0.4   # wait for short responses ≤ 8 words (was 0.7)
SHORT_UTTERANCE_WORDS = 8     # 1.0 s > DOM chunk interval (~0.3-0.5 s)
                              # so rapid chunks still accumulate before flush
MAX_UTTERANCE_SEC     = 30.0  # force-flush after 30 s even if candidate
                              # keeps speaking — prevents 60-90 s buffering

# Python-side backup for UI noise strings that may slip through the
# JS filter when Google Meet splits them across DOM nodes.
_PYTHON_NOISE = [
    "live captions have been turned",
    "captions have been turned",
    "captions are on",
    "captions are off",
    "you left the meeting",
    "meeting ended",
    "has joined the meeting",
    "has left the meeting",
    "joined the meeting",
    "waiting to be admitted",
    "brought you into the call",
    "you've been admitted",
    "turn on captions",
    "turn off captions",
]

# { speaker_name: {"parts": [str], "timer": asyncio.Task | None,
#                  "first_chunk_time": float | None,
#                  "interrupt_sent": bool} }
_utterance_buffers: dict = {}

# Track which speakers already triggered an interrupt this utterance
# to prevent duplicate tts_interrupt signals from both speech_start
# and on_caption paths.
_speech_start_interrupt_sent: set[str] = set()

# Interrupt validation state: {interview_id: {"started_at": float, "initial_text": str, "validated": bool}}
_interrupt_validation: dict = {}

# Configuration imported from caption_scraper
from caption_scraper import INTERRUPT_VALIDATE_DELAY, INTERRUPT_MIN_TEXT_GROWTH
import time


async def _on_speech_start(event):
    """
    Fired by caption_scraper immediately when a speaker's DOM text changes —
    before the stabilization window. Validates interrupt before sending to prevent false positives.

    Validation logic:
    1. Wait INTERRUPT_VALIDATE_DELAY (1.2s) to confirm candidate is STILL speaking
    2. Check if text grew by at least INTERRUPT_MIN_TEXT_GROWTH (3 chars)
    3. Only then send interrupt — prevents false alarms from stale DOM or echo

    Debounced per-utterance via _speech_start_interrupt_sent so that each
    growing word-by-word DOM update does not re-send the interrupt.
    """
    try:
        speaker = event.get("speaker", "Unknown")
        text    = event.get("text", "").strip()
        if "(ai)" in speaker.lower() or speaker.lower() == "you":
            return
        # Already sent for this utterance
        if speaker in _speech_start_interrupt_sent:
            return

        db = mongo_handler.get_db()
        if db is None:
            return

        interview_doc = await db.interviews.find_one(
            {"interview_id": interview_id},
            projection={"bot_speaking": 1},
        )

        bot_speaking = interview_doc and interview_doc.get("bot_speaking") if interview_doc else False

        # Initialize validation state for this interview if not exists
        if interview_id not in _interrupt_validation:
            _interrupt_validation[interview_id] = {
                "started_at": time.monotonic(),
                "initial_text": text,
                "validated": False,
                "last_text": text
            }

        validation = _interrupt_validation[interview_id]
        elapsed = time.monotonic() - validation["started_at"]
        
        # Track text growth to confirm candidate is actively speaking
        text_growth = len(text) - len(validation["initial_text"])
        validation["last_text"] = text

        if bot_speaking:
            # VALIDATION PHASE: Wait 1.2s and check text growth before sending interrupt
            if not validation["validated"]:
                if elapsed < INTERRUPT_VALIDATE_DELAY:
                    # Still validating — wait for more text to confirm real speech
                    return
                elif text_growth < INTERRUPT_MIN_TEXT_GROWTH:
                    # Text didn't grow enough — likely false alarm (stale DOM or echo)
                    msg = f"Interrupt validation failed: text growth={text_growth} chars (need {INTERRUPT_MIN_TEXT_GROWTH})"
                    print(msg); await push_log(msg)
                    # Clear validation state for next attempt
                    del _interrupt_validation[interview_id]
                    return
                else:
                    # Validation passed — mark as validated and send interrupt
                    validation["validated"] = True
                    msg = f"Interrupt validated after {elapsed:.2f}s (growth={text_growth} chars)"
                    print(msg); await push_log(msg)

            # Bot IS speaking + validated → this is a real candidate interruption
            await db.interviews.update_one(
                {"interview_id": interview_id},
                {"$set": {"tts_interrupt": True}},
            )
            _speech_start_interrupt_sent.add(speaker)
            # Also mark the utterance buffer so on_caption doesn't re-send
            if speaker in _utterance_buffers:
                _utterance_buffers[speaker]["interrupt_sent"] = True
            msg = f"Early interrupt signal sent — candidate started speaking ({speaker})"
            print(msg); await push_log(msg)
            # Clear validation state after sending
            if interview_id in _interrupt_validation:
                del _interrupt_validation[interview_id]
            return

        # Bot NOT speaking → clear validation state (no interrupt needed)
        if interview_id in _interrupt_validation:
            del _interrupt_validation[interview_id]

        # Bot NOT speaking → apply echo/stale filters to prevent false interrupts

        # Echo check: the MutationObserver fires ~3 s after TTS starts,
        # carrying the TTS audio as caption text (attributed to "Unknown"
        # or even the candidate's name by Google Meet).  If the incoming
        # text looks like a fragment of recent agent speech, it's an echo
        # — do NOT send the interrupt.
        if text:
            try:
                db_echo = mongo_handler.get_db()
                if db_echo is not None:
                    cutoff = datetime.utcnow() - timedelta(seconds=15)
                    recent = await db_echo.transcripts.find(
                        {"interview_id": interview_id, "speaker": "agent",
                         "timestamp": {"$gte": cutoff}},
                    ).sort("timestamp", -1).limit(5).to_list(5)
                    agent_texts = [r.get("text", "") for r in recent]
                    if _is_echo(text, agent_texts):
                        msg = f"Echo filter (speech_start): suppressed interrupt [{speaker}]: {text[:80]!r}"
                        print(msg); await push_log(msg)
                        return
            except Exception:
                pass  # If check fails, proceed with interrupt

        # Stale DOM check: Google Meet keeps the last caption text in the
        # DOM even after the speaker stops. Any unrelated DOM mutation
        # (e.g. a new participant notification) re-triggers MutationObserver
        # with that stale text — causing a false interrupt right after TTS
        # arms. Suppress if the text is already committed as a candidate
        # transcript (meaning it's old, not new speech).
        if text:
            try:
                db_stale = mongo_handler.get_db()
                if db_stale is not None:
                    last_candidate = await db_stale.transcripts.find(
                        {"interview_id": interview_id, "speaker": "candidate"},
                    ).sort("timestamp", -1).limit(1).to_list(1)
                    if last_candidate:
                        import re as _re
                        def _norm(s):
                            return " ".join(_re.sub(r'[^\w\s]', '', s.lower()).split())
                        n_text = _norm(text)
                        n_committed = _norm(last_candidate[0].get("text", ""))
                        if n_text and n_committed and (
                            n_text in n_committed or n_committed in n_text
                        ):
                            msg = f"Stale DOM filter (speech_start): suppressed interrupt [{speaker}]: {text[:80]!r}"
                            print(msg); await push_log(msg)
                            return
            except Exception:
                pass  # If check fails, proceed with interrupt

    except Exception as e:
        print(f"on_speech_start error (non-fatal): {e}")


async def on_caption(doc):
    try:
        speaker = doc.get("speaker", "Unknown")
        text = doc.get("text", "").strip()

        # Skip the bot's own TTS audio — Chrome labels it as
        # "(AI)" display name, or as "You" (local-user perspective).
        if "(ai)" in speaker.lower() or speaker.lower() == "you" or not text:
            return

        # ── NOISE FILTERS (run BEFORE speaker normalization) ──────
        # These must run first to avoid polluting logs with "candidate: AM bks-ipsc-vak"

        # Skip Google Meet meeting-code artefacts FIRST.
        # "AM abc-def-ghi abc-def-ghi" — 2-3 uppercase prefix + hyphenated code.
        if re.match(r'^[A-Z]{2,3}\s+[a-z]+-[a-z]+-[a-z]+', text):
            msg = f"Noise filter: meet-code skipped: {text!r}"
            print(msg); await push_log(msg)
            return

        # ── Python-side noise backup ──────────────────────────────
        # The JS filter can miss strings when Google Meet splits them across DOM nodes.
        text_lc = text.lower()
        if any(n in text_lc for n in _PYTHON_NOISE):
            msg = f"Noise filter (py): skipped: {text!r}"
            print(msg); await push_log(msg)
            return

        # Skip very short captures that are likely background noise
        # (single punctuation bursts, ambient sound artefacts).
        # Allow single-word genuine answers like "yes", "understood".
        if len(text) <= 2 or (len(text.split()) == 1 and not text[0].isalpha()):
            msg = f"Noise filter: skipped: {text!r}"
            print(msg); await push_log(msg)
            return

        # ── Normalize speaker label ───────────────────────────────
        # Google Meet's speaker detection is unreliable (often "Unknown").
        # We treat ALL non-bot speech as "candidate" — the echo filter
        # uses bot_speaking state + text similarity to filter bot echoes.
        speaker = "candidate"

        # ── Early echo check (all speakers, first chunk only) ─────
        # Run BEFORE buffering on the first chunk of each new utterance.
        # Catches two cases:
        #   - Unknown speaker: TTS audio misattributed before Meet
        #     assigns a name.
        #   - Named speaker: echo arrives 2-4s AFTER bot stops speaking
        #     (Google Meet STT has a processing delay), so bot_speaking
        #     is already False and the echo gate below won't fire.
        # One DB query per utterance start — not per chunk.
        _buf_for_early = _utterance_buffers.get(speaker, {})
        if not _buf_for_early.get("parts"):
            try:
                db = mongo_handler.get_db()
                if db is not None:
                    cutoff = datetime.utcnow() - timedelta(seconds=15)
                    recent = await db.transcripts.find(
                        {"interview_id": interview_id, "speaker": "agent",
                         "timestamp": {"$gte": cutoff}},
                    ).sort("timestamp", -1).limit(10).to_list(10)
                    agent_texts = [r.get("text", "") for r in recent]
                    if _is_echo(text, agent_texts):
                        msg = f"Echo filter (early): discarded [{speaker}]: {text[:80]!r}"
                        print(msg); await push_log(msg)
                        return
            except Exception as ue:
                print(f"Early echo check error (non-fatal): {ue}")

        # ── Interruption signal ───────────────────────────────────
        # Send tts_interrupt=True if bot is currently speaking.
        # This stops TTS playback immediately so candidate can speak.
        buf_peek = _utterance_buffers.get(speaker, {})
        already_sent = (buf_peek.get("interrupt_sent")
                        or speaker in _speech_start_interrupt_sent)
        if not already_sent:
            try:
                db = mongo_handler.get_db()
                if db is not None:
                    interview_doc = await db.interviews.find_one(
                        {"interview_id": interview_id},
                        projection={"bot_speaking": 1},
                    )
                    if interview_doc and interview_doc.get("bot_speaking"):
                        await db.interviews.update_one(
                            {"interview_id": interview_id},
                            {"$set": {"tts_interrupt": True}},
                        )
                        msg = f"Interrupt signal sent (on_caption) — bot was speaking, candidate started"
                        print(msg); await push_log(msg)
                        # Mark both dedup guards so neither path re-sends
                        _speech_start_interrupt_sent.add(speaker)
                        if speaker in _utterance_buffers:
                            _utterance_buffers[speaker]["interrupt_sent"] = True
            except Exception as interrupt_err:
                print(f"Interrupt signal error (non-fatal): {interrupt_err}")

        # NOTE: Echo filtering is done in _flush_speaker() by comparing
        # text similarity with recent agent transcripts. We do NOT discard
        # here based on bot_speaking alone — that would lose candidate
        # speech during legitimate interruptions.

        # Initialise buffer slot for first caption from this speaker
        if speaker not in _utterance_buffers:
            _utterance_buffers[speaker] = {"parts": [], "timer": None, "first_chunk_time": None, "interrupt_sent": False}

        buf = _utterance_buffers[speaker]

        # Record when this utterance started (first chunk only)
        if not buf["parts"]:
            buf["first_chunk_time"] = asyncio.get_event_loop().time()

        buf["parts"].append(text)

        preview = text[:50] + ("..." if len(text) > 50 else "")
        msg = f"Buffering [{speaker}]: {preview}"
        print(msg); await push_log(msg)

        # Force-flush if the candidate has been speaking for MAX_UTTERANCE_SEC
        # without a long-enough pause (prevents 60-90 s buffering).
        elapsed = asyncio.get_event_loop().time() - (buf["first_chunk_time"] or asyncio.get_event_loop().time())
        if elapsed >= MAX_UTTERANCE_SEC:
            if buf["timer"] and not buf["timer"].done():
                buf["timer"].cancel()
            buf["timer"] = asyncio.create_task(
                _flush_speaker(speaker, buf, force=True)
            )
        else:
            # Cancel previous silence timer and start a fresh one
            if buf["timer"] and not buf["timer"].done():
                buf["timer"].cancel()
            buf["timer"] = asyncio.create_task(
                _flush_speaker(speaker, buf)
            )

    except Exception as e:
        msg = f"Caption processing error: {e}"
        print(msg); await push_log(msg)


async def _flush_speaker(speaker: str, buf: dict, force: bool = False) -> None:
    """
    Flush a speaker's buffered utterance to MongoDB.

    Called by the silence timer after UTTERANCE_SILENCE_SEC of no new chunks.
    If force=True (MAX_UTTERANCE_SEC reached), flush immediately regardless of timer.
    """
    full_text = " ".join(buf["parts"]).strip()
    if not full_text:
        return

    # Echo check: compare with recent agent transcripts to filter
    # out the bot's own TTS audio that Chrome captions as "Unknown"
    # or even the candidate's name (Google Meet STT echo).
    # Only run this check if bot_speaking was True recently.
    db_echo = mongo_handler.get_db()
    if db_echo is not None:
        cutoff = datetime.utcnow() - timedelta(seconds=15)
        recent = await db_echo.transcripts.find(
            {"interview_id": interview_id, "speaker": "agent",
             "timestamp": {"$gte": cutoff}},
        ).sort("timestamp", -1).limit(5).to_list(5)
        agent_texts = [r.get("text", "") for r in recent]
        if _is_echo(full_text, agent_texts):
            msg = f"Echo filter (bot_speaking): skipped [{speaker}]: {full_text[:80]!r}"
            print(msg); await push_log(msg)
            buf["parts"] = []
            return

    # Dedup check: skip if this exact text was saved in the last 60s
    # (prevents double-saving from DOM + Web Speech API).
    db_dedup = mongo_handler.get_db()
    if db_dedup is not None:
        cutoff = datetime.utcnow() - timedelta(seconds=60)
        recent = await db_dedup.transcripts.find(
            {"interview_id": interview_id, "speaker": "candidate",
             "timestamp": {"$gte": cutoff}},
        ).sort("timestamp", -1).limit(5).to_list(5)
        import re as _re
        def _norm(s):
            return " ".join(_re.sub(r'[^\w\s]', '', s.lower()).split())
        n_full = _norm(full_text)
        for r in recent:
            n_prev = _norm(r.get("text", ""))
            if n_full and n_prev and (n_full in n_prev or n_prev in n_full):
                msg = f"Dedup filter: skipped [{speaker}] (duplicate of recent): {full_text[:60]!r}"
                print(msg); await push_log(msg)
                buf["parts"] = []
                return

    # Save to MongoDB
    if mongo_connected:
        await insert_transcript(interview_id, "candidate", full_text)
        preview = full_text[:80] + ("..." if len(full_text) > 80 else "")
        msg = f"Saved turn [{speaker}]: {preview}"
        print(msg); await push_log(msg)


def _is_echo(text: str, agent_texts: list[str]) -> bool:
    """
    Check if `text` looks like an echo of recent agent speech.

    Uses word overlap (Jaccard similarity) to detect phonetic variations
    that STT may produce (e.g. "Agentic AI" → "a genetic AI").

    Returns True if overlap > 0.85 (configurable threshold).
    """
    import re
    def _words(s):
        return set(re.sub(r'[^\w\s]', '', s.lower()).split())

    text_words = _words(text)
    if not text_words:
        return False

    for agent_text in agent_texts:
        agent_words = _words(agent_text)
        if not agent_words:
            continue

        # Jaccard similarity: |A ∩ B| / |A ∪ B|
        intersection = len(text_words & agent_words)
        union = len(text_words | agent_words)
        if union == 0:
            continue

        similarity = intersection / union
        if similarity > 0.85:  # Threshold for echo detection
            return True

    return False


async def _abort(reason: str) -> None:
    """Mark the interview as abandoned and exit."""
    db = mongo_handler.get_db()
    if db is not None:
        await db.interviews.update_one(
            {"interview_id": interview_id},
            {"$set": {
                "status": "abandoned",
                "ended_at": datetime.utcnow(),
                "limits": {"forced_end_reason": reason}
            }}
        )


# Import os for environment variables
import os
