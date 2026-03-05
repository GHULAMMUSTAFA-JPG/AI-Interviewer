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
from datetime import datetime
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
                        await ctx.close()
                        return
                except Exception:
                    pass

            if not admitted:
                msg = "Not admitted (10 min timeout)"
                print(msg); await push_log(msg)
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
            # The caption scraper fires on_caption once per *stabilized chunk*
            # (~1.5 s of DOM silence), not once per full turn. A candidate
            # saying three sentences would produce three DB inserts, causing
            # Main-Agent to respond mid-thought three times.
            #
            # Fix: buffer chunks per speaker. Start a 4-second silence timer
            # on each new chunk. If more text arrives, cancel and restart the
            # timer. When the timer actually fires (real silence), flush the
            # entire accumulated turn as ONE DB insert.
            # ─────────────────────────────────────────────────────────────
            UTTERANCE_SILENCE_SEC = 1.2   # wait for long/mid responses (was 2.0)
            SHORT_UTTERANCE_SEC   = 0.7   # wait for short responses ≤ 8 words (was 1.0)
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

            # Tracks speakers for which an early interrupt was already sent this
            # utterance (via on_speech_start). Cleared when the utterance flushes.
            _speech_start_interrupt_sent: set = set()

            def _is_echo(text: str, agent_texts: list) -> bool:
                """
                Return True if text is likely the bot's TTS audio being echoed back
                through the meeting captions.

                Two checks:
                  1. Substring: candidate text appears verbatim inside a recent agent
                     message (after normalisation) → near-certain echo.
                  2. Word-overlap: ≥ 65 % of candidate words are in a recent agent
                     message — only applied when candidate text is ≥ 8 words to
                     avoid false positives on short genuine answers.
                """
                if not text or not agent_texts:
                    return False
                norm = " ".join(text.lower().split())
                words = norm.split()
                for agent_text in agent_texts:
                    agent_norm = " ".join(agent_text.lower().split())
                    # Substring match (strong signal)
                    if norm in agent_norm:
                        return True
                    # Word-overlap (only for longer candidate speech).
                    # Threshold is 85% (not 65%) — when the agent asks about
                    # topic X, the candidate naturally uses those same words.
                    # A genuine answer about "AI agents, software development"
                    # would hit 65%+ easily; real TTS echo is typically 90%+.
                    if len(words) >= 8:
                        agent_words = set(agent_norm.split())
                        overlap = len(set(words) & agent_words) / len(words)
                        if overlap >= 0.85:
                            return True
                return False

            async def _flush_speaker(speaker: str, buf: dict, force: bool = False) -> None:
                """
                Wait for silence, then write the full utterance to DB.

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
                    # Check whether this text is the bot's own TTS audio being
                    # misattributed by Google Meet to the candidate speaker.
                    try:
                        db = mongo_handler.get_db()
                        if db is not None:
                            recent = await db.transcripts.find(
                                {"interview_id": interview_id, "speaker": "agent"},
                            ).sort("timestamp", -1).limit(10).to_list(10)
                            agent_texts = [r.get("text", "") for r in recent]
                            if _is_echo(full_text, agent_texts):
                                msg = f"Echo filter: skipped [{speaker}]: {full_text[:80]!r}"
                                print(msg); await push_log(msg)
                                return
                    except Exception as echo_err:
                        print(f"Echo filter error (non-fatal): {echo_err}")

                    await insert_transcript(interview_id, "candidate", full_text)
                    preview = full_text[:80] + ("..." if len(full_text) > 80 else "")
                    msg = f"Saved turn [{speaker}]: {preview}"
                    print(msg); await push_log(msg)
                except asyncio.CancelledError:
                    pass  # New caption arrived — timer was reset, not an error

            async def _on_speech_start(event):
                """
                Fired by caption_scraper immediately when a speaker's DOM text changes —
                before the 1.5 s stabilization window. Sends the TTS interrupt signal
                ~1.5 s earlier than the on_caption path.

                Debounced per-utterance via _speech_start_interrupt_sent so that each
                growing word-by-word DOM update does not re-send the interrupt.
                """
                try:
                    speaker = event.get("speaker", "Unknown")
                    if "(ai)" in speaker.lower():
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
                    if interview_doc and interview_doc.get("bot_speaking"):
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
                except Exception as e:
                    print(f"on_speech_start error (non-fatal): {e}")

            async def on_caption(doc):
                try:
                    speaker = doc.get("speaker", "Unknown")
                    text = doc.get("text", "").strip()

                    # Skip the bot's own TTS audio — Chrome picks it up as
                    # captions attributed to the bot's display name which ends
                    # in " (AI)".
                    if "(ai)" in speaker.lower() or not text:
                        return

                    # ── Python-side noise backup ──────────────────────────────
                    # The JS layer filters _UI_NOISE but can miss strings when
                    # Google Meet splits them across DOM nodes. Check here too.
                    text_lc = text.lower()
                    if any(n in text_lc for n in _PYTHON_NOISE):
                        msg = f"Noise filter (py): skipped [{speaker}]: {text!r}"
                        print(msg); await push_log(msg)
                        return

                    # Skip very short captures that are likely background noise
                    # (single punctuation bursts, ambient sound artefacts).
                    # Allow single-word genuine answers like "yes", "understood".
                    if len(text) <= 2 or (len(text.split()) == 1 and not text[0].isalpha()):
                        msg = f"Noise filter: skipped [{speaker}]: {text!r}"
                        print(msg); await push_log(msg)
                        return

                    # Skip Google Meet meeting-code artefacts.
                    # "AM abc-def-ghi abc-def-ghi" — 2-3 uppercase prefix + hyphenated code.
                    if re.match(r'^[A-Z]{2,3}\s+[a-z]+-[a-z]+-[a-z]+', text):
                        msg = f"Noise filter: meet-code skipped [{speaker}]: {text!r}"
                        print(msg); await push_log(msg)
                        return

                    # ── Unknown speaker: early echo check (Option A) ──────────
                    # When Google Meet hasn't attributed audio to a named speaker
                    # yet, the caption arrives as "Unknown". This is the most
                    # common path for bot TTS audio slipping through the "(AI)"
                    # filter. Run the echo check NOW before buffering, so we
                    # discard it immediately rather than waiting for flush.
                    if speaker == "Unknown":
                        try:
                            db = mongo_handler.get_db()
                            if db is not None:
                                recent = await db.transcripts.find(
                                    {"interview_id": interview_id, "speaker": "agent"},
                                ).sort("timestamp", -1).limit(10).to_list(10)
                                agent_texts = [r.get("text", "") for r in recent]
                                if _is_echo(text, agent_texts):
                                    msg = f"Echo filter (Unknown): discarded: {text[:80]!r}"
                                    print(msg); await push_log(msg)
                                    return
                        except Exception as ue:
                            print(f"Unknown echo check error (non-fatal): {ue}")

                    # ── Interruption signal ───────────────────────────────────
                    # If the bot is currently speaking (TTS is playing audio),
                    # tell TTS to stop immediately by setting tts_interrupt=True.
                    # Fires on the FIRST caption chunk from this utterance only.
                    # Also checks _speech_start_interrupt_sent to avoid a double
                    # send if _on_speech_start already fired for this utterance.
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
