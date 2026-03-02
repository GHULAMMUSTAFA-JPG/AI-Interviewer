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
    Background task: polls interviews.interviews every 5 s. When status becomes
    'abandoned' or 'completed', clicks the Leave call button and exits — which
    cancels the sibling tasks (caption scraper + audio watcher).
    """
    while True:
        await asyncio.sleep(5)
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
            UTTERANCE_SILENCE_SEC = 2.0

            # { speaker_name: {"parts": [str], "timer": asyncio.Task | None} }
            _utterance_buffers: dict = {}

            async def _flush_speaker(speaker: str, buf: dict) -> None:
                """Wait for silence, then write the full utterance to DB."""
                try:
                    await asyncio.sleep(UTTERANCE_SILENCE_SEC)
                    full_text = " ".join(buf["parts"]).strip()
                    buf["parts"] = []
                    buf["timer"] = None
                    if full_text and mongo_connected:
                        await insert_transcript(interview_id, "candidate", full_text)
                        preview = full_text[:80] + ("..." if len(full_text) > 80 else "")
                        msg = f"Saved turn [{speaker}]: {preview}"
                        print(msg); await push_log(msg)
                except asyncio.CancelledError:
                    pass  # New caption arrived — timer was reset, not an error

            async def on_caption(doc):
                try:
                    speaker = doc.get("speaker", "Unknown")
                    text = doc.get("text", "").strip()

                    # Skip the bot's own TTS audio — Chrome picks it up as
                    # captions attributed to the bot's display name which ends
                    # in " (AI)".
                    if "(ai)" in speaker.lower() or not text:
                        return

                    # Skip very short captures that are likely background noise
                    # (single words, punctuation bursts, ambient sound artefacts).
                    if len(text) < 10 and len(text.split()) < 3:
                        msg = f"Noise filter: skipped [{speaker}]: {text!r}"
                        print(msg); await push_log(msg)
                        return

                    # Skip Google Meet meeting-code artefacts.
                    # These appear as "AM abc-def-ghi abc-def-ghi" — the meeting
                    # URL slug repeated twice with an "AM" prefix.
                    # Pattern: 2–3 letter prefix + hyphenated code (repeated).
                    if re.match(r'^[A-Z]{2,3}\s+[a-z]+-[a-z]+-[a-z]+', text):
                        msg = f"Noise filter: meet-code skipped [{speaker}]: {text!r}"
                        print(msg); await push_log(msg)
                        return

                    # ── Interruption signal ───────────────────────────────────
                    # If the bot is currently speaking (TTS is playing audio),
                    # tell TTS to stop immediately by setting tts_interrupt=True.
                    # TTS watches for this flag via a MongoDB change stream and
                    # stops the pacat audio stream mid-chunk.
                    # This fires on the FIRST caption chunk so the bot stops
                    # as soon as the candidate opens their mouth — before the
                    # full utterance is even buffered.
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
                                msg = f"Interrupt signal sent — bot was speaking, candidate started"
                                print(msg); await push_log(msg)
                    except Exception as interrupt_err:
                        print(f"Interrupt signal error (non-fatal): {interrupt_err}")

                    # Initialise buffer slot for first caption from this speaker
                    if speaker not in _utterance_buffers:
                        _utterance_buffers[speaker] = {"parts": [], "timer": None}

                    buf = _utterance_buffers[speaker]
                    buf["parts"].append(text)

                    preview = text[:50] + ("..." if len(text) > 50 else "")
                    msg = f"Buffering [{speaker}]: {preview}"
                    print(msg); await push_log(msg)

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
                scrape_meeting_captions(page, session_id, on_caption)
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
