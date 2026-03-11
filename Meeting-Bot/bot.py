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
import time
from pathlib import Path
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

from caption_scraper import scrape_meeting_captions, INTERRUPT_VALIDATE_DELAY, INTERRUPT_MIN_TEXT_GROWTH
from logger import push_log
import mongo_handler
from mongo_handler import (
    connect_to_mongo,
    disconnect_from_mongo,
    insert_transcript,
)


async def _watch_bot_speaking_for_stt(page, session_id: str) -> None:
    """
    Watch interviews.interviews for bot_speaking transitions to pause/resume STT.
    
    When TTS starts (bot_speaking → True):
        Call page.evaluate('window.__pauseSTT()') so Chrome stops capturing audio.
        This prevents the bot from hearing its own TTS output as candidate speech.
    
    When TTS finishes (bot_speaking → False):
        Call page.evaluate('window.__resumeSTT()') to restart STT listening.
    """
    import os as _os
    from motor.motor_asyncio import AsyncIOMotorClient as _MotorClient
    
    mongo_uri = _os.getenv('MONGODB_URI', 'mongodb://mongodb:27017/?replicaSet=rs0')
    db_name = _os.getenv('MONGODB_DB', 'interviews')
    
    try:
        client = _MotorClient(mongo_uri, serverSelectionTimeoutMS=5000)
        db = client[db_name]
    except Exception as e:
        msg = f'[STT-CTRL] MongoDB connection failed: {e} — bot_speaking watcher skipped'
        print(msg); await push_log(msg)
        return
    
    pipeline = [{'$match': {'operationType': 'update'}}]
    
    while True:
        try:
            msg = f'[STT-CTRL] change stream (re)started for session={session_id}'
            print(msg); await push_log(msg)
            
            async with db['interviews'].watch(
                pipeline, full_document='updateLookup'
            ) as stream:
                async for change in stream:
                    full_doc = change.get('fullDocument') or {}
                    if str(full_doc.get('interview_id', '')) != session_id:
                        continue
                    
                    updated = (change.get('updateDescription') or {}).get('updatedFields', {})
                    if 'bot_speaking' not in updated:
                        continue
                    
                    is_speaking = updated['bot_speaking']
                    try:
                        if is_speaking:
                            msg = '[STT-CTRL] bot_speaking=True → pausing STT'
                            print(msg); await push_log(msg)
                            await page.evaluate('window.__pauseSTT && window.__pauseSTT()')
                        else:
                            msg = '[STT-CTRL] bot_speaking=False → resuming STT'
                            print(msg); await push_log(msg)
                            await page.evaluate('window.__resumeSTT && window.__resumeSTT()')
                    except Exception as _pe:
                        msg = f'[STT-CTRL] page.evaluate error: {_pe}'
                        print(msg); await push_log(msg)
                        
        except asyncio.CancelledError:
            # Task cancelled (meeting ended) — always resume STT before exiting
            try:
                await page.evaluate('window.__resumeSTT && window.__resumeSTT()')
            except Exception:
                pass
            return
        except Exception as exc:
            msg = f'[STT-CTRL] watcher error: {exc} — reconnecting in 3s'
            print(msg); await push_log(msg)
            # Safety: if watcher crashes while bot was speaking, un-pause STT
            try:
                await page.evaluate('window.__resumeSTT && window.__resumeSTT()')
            except Exception:
                pass
            await asyncio.sleep(3.0)


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
                print(msg)
                await push_log(msg)

                for sel in [
                    'button[aria-label="Leave call"]',
                    'button[aria-label*="Leave" i]',
                    'button[data-tooltip*="leave" i]',
                ]:
                    try:
                        btn = page.locator(sel)
                        if await btn.count() > 0 and await btn.first.is_visible():
                            await btn.first.click()
                            break
                    except Exception:
                        pass

                # Guaranteed fallback: navigate away from the Meet URL so Chrome
                # disconnects from the meeting even if the button click failed or
                # a confirmation dialog was shown.
                try:
                    await page.goto("about:blank", timeout=5000)
                except Exception:
                    pass

                return  # completes the task; asyncio.wait cancels siblings

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"Leave watcher error: {e}")


async def join_meeting(url: str, email: str, interview_id: str, headless: bool = False):
    """
    Join Google Meet as guest, scrape captions, and save candidate speech to DB.

    Args:
        url:          Google Meet URL
        email:        Bot guest email (display name = email prefix + " (AI)")
        interview_id: interviews.interviews _id / interview_id field
        headless:     Whether to run Chrome headlessly (False for dev, True for prod)
    """
    # Persistent Chrome profile — survives container restarts.
    # Mount /app/chrome_profile as a Docker volume so login/cookies persist.
    PROFILE_DIR = "/app/chrome_profile"
    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)

    # Remove stale Chrome lock files left behind by a previous crash or
    # unclean shutdown.  If these files exist, launch_persistent_context
    # cannot acquire the profile lock and silently falls back to a fresh
    # (unlogged-in) profile, losing all saved cookies and Gmail login.
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = Path(PROFILE_DIR) / lock_name
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = email.split("@")[0]

    recordings_dir = Path(__file__).parent / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)

    msg = f"Bot: {name} (AI)  interview_id={interview_id}"
    print(msg); await push_log(msg)

    mongo_connected = await connect_to_mongo()
    if not mongo_connected:
        msg = "Continuing without MongoDB (captions will not be saved)"
        print(msg); await push_log(msg)

    async def _abort(reason: str) -> None:
        """Mark interview abandoned and log. Called on every early-exit path."""
        try:
            _db = mongo_handler.get_db()
            if _db is not None:
                await _db.interviews.update_one(
                    {"interview_id": interview_id, "status": "in_progress"},
                    {"$set": {"status": "abandoned", "ended_at": datetime.utcnow()}}
                )
        except Exception:
            pass
        msg2 = f"Interview {interview_id} aborted: {reason}"
        print(msg2); await push_log(msg2)

    async with async_playwright() as p:
        try:
            chrome_args = [
                "--use-fake-ui-for-media-stream",  # Bypass camera/mic permission prompts
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--window-position=0,0",
                "--window-size=1280,720",         # Match Xvfb display resolution
                "--disable-infobars",
                "--mute-audio",              # Mute speaker output (we don't need to hear)
                "--disable-camera-input",
                "--no-sandbox",              # Required when running as root in Docker
                "--disable-dev-shm-usage",   # Use /tmp instead of /dev/shm (avoids OOM in Docker)
                "--disable-gpu",             # No GPU in Docker
                # NOTE: --use-fake-device-for-media-stream is intentionally absent.
                # Chrome uses the real PulseAudio virtual_mic_source as its microphone,
                # which is where TTS plays its audio. Removing this flag is what makes
                # the agent's voice audible to meeting participants.
            ]

            msg = "Launching browser..."
            print(msg); await push_log(msg)

            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=headless,
                channel="chrome",
                viewport={"width": 1280, "height": 720},
                args=chrome_args,
                env={"DISPLAY": ":99"},  # Explicit Xvfb display — makes Chrome visible in VNC
                accept_downloads=False,
                ignore_default_args=["--enable-automation"],
            )

            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            
            # ── Console event handler — capture Web Speech API transcripts ─────
            # Web Speech API emits console.log events:
            #   TRANSCRIPT_EVENT:<text>  — candidate speech (final)
            #   STT_SPEECH_START:        — candidate started speaking (for interrupt)
            #   STT_INTERIM:<text>       — partial transcript (not saved)
            async def _handle_console(msg):
                try:
                    text = msg.text
                    msg_type = msg.type
                    
                    # Check for Web Speech API transcript events
                    if text.startswith('TRANSCRIPT_EVENT:'):
                        candidate_text = text[len('TRANSCRIPT_EVENT:'):].strip()
                        if candidate_text:
                            # Process as candidate speech (same as DOM caption path)
                            await on_caption({
                                'speaker': 'candidate',
                                'text': candidate_text,
                                'timestamp': datetime.utcnow().isoformat()
                            })
                    
                    # Check for speech start events (for interrupt detection)
                    elif text.startswith('STT_SPEECH_START:'):
                        # Candidate started speaking via Web Speech API
                        # Trigger interrupt if bot is speaking
                        if _interrupt_validation.get(interview_id, {}).get('last_text'):
                            await _on_speech_start({
                                'speaker': 'candidate',
                                'text': _interrupt_validation[interview_id]['last_text']
                            })
                    
                    # Log other STT events for debugging
                    elif text.startswith('STT_') or '[STT]' in text:
                        msg = f"🎤 {text[:200]}"
                        print(msg); await push_log(msg)
                        
                except Exception as e:
                    # Don't let console handler errors break the bot
                    print(f"Console handler error: {e}")
            
            page.on('console', _handle_console)
            page.on('pageerror', lambda err: print(f"[PAGE ERROR] {err}"))

            msg = f"Navigating to {url}"
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

            # Enter guest name — bot appears as "<name> (AI)" so caption filter works
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
                msg = "No join button found"
                print(msg); await push_log(msg)
                await _abort("join button not found")
                await ctx.close()
                return

            # Wait for admittance
            msg = "Waiting for host to admit..."
            print(msg); await push_log(msg)
            admitted = False
            for _ in range(600):  # 10 minutes
                await page.wait_for_timeout(1000)
                try:
                    if await page.locator('button[aria-label="Leave call"]').count() > 0:
                        admitted = True
                        break
                    body = await page.evaluate("() => document.body.innerText.toLowerCase()")
                    if any(x in body for x in ["not found", "has ended", "denied", "can't access"]):
                        msg = "Denied / Meeting ended before join"
                        print(msg); await push_log(msg)
                        await _abort("denied or meeting ended")
                        await ctx.close()
                        return
                except Exception:
                    pass
                # Check if interview was stopped while waiting in lobby
                try:
                    _lobby_db = mongo_handler.get_db()
                    if _lobby_db is not None:
                        _lobby_check = await _lobby_db.interviews.find_one(
                            {"interview_id": interview_id},
                            projection={"status": 1}
                        )
                        if _lobby_check and _lobby_check.get("status") in ("abandoned", "completed"):
                            msg = f"Interview {interview_id} stopped while in lobby — aborting"
                            print(msg); await push_log(msg)
                            await ctx.close()
                            return
                except Exception:
                    pass

            if not admitted:
                msg = "Not admitted (10 min timeout)"
                print(msg); await push_log(msg)
                await _abort("not admitted within 10 minutes")
                await ctx.close()
                return

            msg = "Admitted to meeting!"
            print(msg); await push_log(msg)
            await page.wait_for_timeout(1500)

            # Post-admit: camera still off
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

            # Dismiss popups
            await page.wait_for_timeout(2000)
            for _ in range(3):
                try:
                    for label in ["Got it", "Dismiss", "OK", "Close"]:
                        btn = page.locator(f"button:has-text('{label}')")
                        if await btn.count() > 0 and await btn.first.is_visible():
                            await btn.first.click()
                            await page.wait_for_timeout(500)
                except Exception:
                    pass
                await page.wait_for_timeout(1000)

            await page.wait_for_timeout(3000)

            # Ensure microphone is ACTIVE (not muted) in Google Meet.
            # If the meeting host or browser defaulted to muted, click Unmute.
            # The bot must be unmuted so TTS audio (PulseAudio → virtual_mic_source
            # → Chrome getUserMedia) actually reaches meeting participants.
            try:
                for mic_sel in [
                    'button[aria-label="Unmute microphone"]',
                    'button[aria-label*="Unmute" i][aria-label*="microphone" i]',
                    'button[data-is-muted="true"][aria-label*="microphone" i]',
                ]:
                    mic_btn = page.locator(mic_sel)
                    if await mic_btn.count() > 0 and await mic_btn.first.is_visible():
                        await mic_btn.first.click()
                        await page.wait_for_timeout(500)
                        msg = "Microphone unmuted"
                        print(msg); await push_log(msg)
                        break
                else:
                    msg = "Microphone already active (or unmute button not found)"
                    print(msg); await push_log(msg)
            except Exception as e:
                print(f"Unmute check error: {e}")

            await page.wait_for_timeout(1000)

            # Signal that Chrome is live in the meeting and transmitting audio.
            db = mongo_handler.get_db()
            if db is not None:
                await db.interviews.update_one(
                    {"interview_id": interview_id},
                    {"$set": {"bot_status": "admitted"}}
                )
                msg = "Interview updated: bot_status=admitted"
                print(msg); await push_log(msg)

            msg = "Starting caption scraping..."
            print(msg); await push_log(msg)

            # ── Utterance buffer ──────────────────────────────────────────
            # Optimized for faster conversation flow
            UTTERANCE_SILENCE_SEC = 0.5   # wait for responses (reduced from 0.8)
            SHORT_UTTERANCE_SEC   = 0.3   # wait for short responses ≤ 8 words
            SHORT_UTTERANCE_WORDS = 8
            MAX_UTTERANCE_SEC     = 30.0  # force-flush after 30 s

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

            # Tracks speakers for which an early interrupt was already sent this
            # utterance (via on_speech_start). Cleared when the utterance flushes.
            _speech_start_interrupt_sent: set = set()

            def _is_echo(text: str, agent_texts: list) -> bool:
                """
                Return True if text is likely the bot's TTS audio being echoed back
                through the meeting captions.

                Google Meet STT changes punctuation between the original TTS text and
                the echo (e.g. "interviewer!" → "interviewer.", "challenges" →
                "challenges?"). Strip punctuation before comparison so token matching
                is not broken by punctuation differences.

                Two checks:
                  1. Substring: candidate text appears verbatim inside a recent agent
                     message → near-certain echo.
                  2. Word-overlap: ≥ 80% of candidate words match a recent agent
                     message, for utterances ≥ 8 words.
                """
                if not text or not agent_texts:
                    return False

                def _strip(s: str) -> str:
                    # Remove all punctuation, lowercase, collapse whitespace.
                    return " ".join(re.sub(r'[^\w\s]', '', s.lower()).split())

                norm  = _strip(text)
                words = norm.split()
                for agent_text in agent_texts:
                    agent_norm = _strip(agent_text)
                    if norm in agent_norm:
                        return True
                    if len(words) >= 8:
                        agent_words = set(agent_norm.split())
                        overlap = len(set(words) & agent_words) / len(words)
                        if overlap >= 0.85:  # catches STT phonetic variations (e.g. "Agentic AI" → "a genetic AI")
                            return True
                return False

            async def _flush_speaker(speaker: str, buf: dict, force: bool = False) -> None:
                """
                Wait for silence, then write the full utterance to DB.

                CRITICAL: Speaker labels from Google Meet are unreliable (often "Unknown").
                We use bot_speaking state + text similarity to filter echoes, NOT speaker names.

                Wait duration is adaptive:
                  - Short responses (≤ SHORT_UTTERANCE_WORDS words): SHORT_UTTERANCE_SEC
                  - Longer responses: UTTERANCE_SILENCE_SEC
                If more text arrives during the wait the timer is cancelled and
                restarted, so the candidate can always extend a short answer.

                force=True skips the sleep (used when MAX_UTTERANCE_SEC is hit).
                """
                try:
                    if not force:
                        current = " ".join(buf["parts"]).strip()
                        wait = (SHORT_UTTERANCE_SEC
                                if len(current.split()) <= SHORT_UTTERANCE_WORDS
                                else UTTERANCE_SILENCE_SEC)
                        await asyncio.sleep(wait)

                    full_text = " ".join(buf["parts"]).strip()
                    buf["parts"] = []
                    buf["timer"] = None
                    buf["first_chunk_time"] = None
                    buf["interrupt_sent"] = False  # Reset for next utterance
                    _speech_start_interrupt_sent.discard(speaker)  # Reset early interrupt state
                    if not full_text or not mongo_connected:
                        return

                    # ── Echo filter ───────────────────────────────────────────
                    # CRITICAL: Check bot_speaking state FIRST, then text similarity.
                    # Text similarity alone fails because agent transcripts are saved
                    # AFTER TTS completes, but captions arrive WHILE TTS is playing.
                    try:
                        db = mongo_handler.get_db()
                        if db is not None:
                            # Check if bot is CURRENTLY speaking - if yes, this is definitely echo
                            interview_doc = await db.interviews.find_one(
                                {"interview_id": interview_id},
                                projection={"bot_speaking": 1},
                            )
                            if interview_doc and interview_doc.get("bot_speaking"):
                                msg = f"Echo filter (bot_speaking): skipped [{speaker}]: {full_text[:80]!r}"
                                print(msg); await push_log(msg)
                                return
                            
                            # Also check text similarity for delayed echoes (bot just finished)
                            cutoff = datetime.utcnow() - timedelta(seconds=15)
                            recent = await db.transcripts.find(
                                {"interview_id": interview_id, "speaker": "agent",
                                 "timestamp": {"$gte": cutoff}},
                            ).sort("timestamp", -1).limit(10).to_list(10)
                            agent_texts = [r.get("text", "") for r in recent]
                            if _is_echo(full_text, agent_texts):
                                msg = f"Echo filter: skipped [{speaker}]: {full_text[:80]!r}"
                                print(msg); await push_log(msg)
                                return
                    except Exception as echo_err:
                        print(f"Echo filter error (non-fatal): {echo_err}")

                    # ── Dedup guard ───────────────────────────────────────────────
                    # Google Meet re-attributes mid-utterance (Unknown → "Ghulam Ahmed"),
                    # creating multiple buffer slots that all flush the same text.
                    # Skip if identical text was already saved in the last 60 seconds.
                    try:
                        db_dedup = mongo_handler.get_db()
                        if db_dedup is not None:
                            cutoff = datetime.utcnow() - timedelta(seconds=60)
                            recent_cands = await db_dedup.transcripts.find(
                                {"interview_id": interview_id, "speaker": "candidate",
                                 "timestamp": {"$gte": cutoff}}
                            ).sort("timestamp", -1).limit(5).to_list(5)
                            def _norm_d(s):
                                return " ".join(re.sub(r'[^\w\s]', '', s.lower()).split())
                            n_new = _norm_d(full_text)
                            for rec in recent_cands:
                                n_old = _norm_d(rec.get("text", ""))
                                if n_new and n_old and (n_new == n_old or n_new in n_old or n_old in n_new):
                                    msg = f"Dedup filter: skipped [{speaker}] (duplicate of recent): {full_text[:60]!r}"
                                    print(msg); await push_log(msg)
                                    return
                    except Exception as dd_err:
                        print(f"Dedup check error (non-fatal): {dd_err}")

                    await insert_transcript(interview_id, "candidate", full_text)
                    preview = full_text[:80] + ("..." if len(full_text) > 80 else "")
                    msg = f"Saved turn [{speaker}]: {preview}"
                    print(msg); await push_log(msg)
                except asyncio.CancelledError:
                    pass  # New caption arrived — timer was reset, not an error

            # Track interrupt validation state per interview
            _interrupt_validation = {}  # {interview_id: {"started_at": float, "initial_text": str, "validated": bool, "last_text": str}}

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
                    # The JS layer filters _UI_NOISE but can miss strings when
                    # Google Meet splits them across DOM nodes. Check here too.
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

            # Run caption scraper and leave watcher concurrently.
            scrape_task = asyncio.create_task(
                scrape_meeting_captions(page, session_id, on_caption, _on_speech_start)
            )
            
            # Web Speech API disabled - using DOM scraping only (more reliable)
            stt_ctrl_task = None
            
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

            try:
                await ctx.close()
            except Exception:
                pass

        except Exception as e:
            msg = f"Fatal error in join_meeting: {e}"
            print(msg); await push_log(msg)
            try:
                await ctx.close()
            except Exception:
                pass

        finally:
            await disconnect_from_mongo()
