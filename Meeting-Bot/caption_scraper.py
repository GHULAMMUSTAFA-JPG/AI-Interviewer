"""
Caption Scraper for Google Meet
─────────────────────────────────────────────────────────────────────
DOM-ONLY caption extraction.
No microphone. No Web Speech API. No getUserMedia. No audio capture.

This scraper reads the visual caption overlay that Google Meet
renders on-screen when captions are enabled by the user/host.

═══════════════════════════════════════════════════════════════════
Why rolling incremental captions happen
───────────────────────────────────────
Google Meet updates captions word-by-word inside a single DOM node:
    poll 1 → "Hello"
    poll 2 → "Hello how"
    poll 3 → "Hello how are"
    poll 4 → "Hello how are you."

If we saved every poll result we would store 4 rows for one sentence.
Fix: buffer each speaker's latest text; only commit after the text
has been STABLE (unchanged) for STABILIZATION_SEC seconds, meaning
the speaker has finished their sentence.

═══════════════════════════════════════════════════════════════════
Why "Unknown" duplicates appear
────────────────────────────────
Google Meet sometimes renders caption text before the name widget
loads, so we label it "Unknown". Moments later the same text
re-appears attributed to the real speaker.
Fix: hold Unknown entries in a pending dict. If the same text
arrives under a real name, discard the Unknown entry. If no real
name arrives within 2× the stabilization window, commit Unknown
as a fallback.

═══════════════════════════════════════════════════════════════════
Why hash-based dedup is needed
───────────────────────────────
Multiple DOM selectors (aria-live, jsname spans, aria-atomic, etc.)
can all match the same visible caption text in different containers.
Fix: SHA-1 hash every committed text. Skip writing if hash already
seen, regardless of which selector found it.
─────────────────────────────────────────────────────────────────────
"""

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

from logger import push_log

# ── Configuration ──────────────────────────────────────────────────
POLL_INTERVAL          = 0.5   # seconds between DOM polls (was 1.0 — halved for faster interrupt detection)
STABILIZATION_SEC      = 0.7   # seconds a caption must be unchanged before saving (was 1.0)
SILENT_WARN_SEC        = 30    # log a warning after this many silent seconds
ENABLE_RETRIES         = 5     # caption-toggle attempts
TRANSCRIPT_DIR         = Path("transcription_recordings")
HASH_DEDUP_WINDOW_SEC  = 15.0  # same text within 15 s = duplicate; after 15 s = treat as new
SPEAKER_RESET_SEC      = 20.0  # if speaker silent this long, allow same text to be re-committed

# Strings that identify Google Meet UI messages — NOT spoken captions.
# Any extracted text containing one of these sub-strings is discarded.
_UI_NOISE = [
    "your microphone",
    "your camera",
    "submit feedback",
    "turn on captions",
    "turn off captions",
    "captions are on",
    "captions are off",
    "you're presenting",
    "share your screen",
    "recording",
    "live stream",
    "meeting details",
    "got it",
    "dismiss",
    "learn more",
    "please wait",
    "waiting to be admitted",
    "brought you into the call",
    "you've been admitted",
    "host has not started",
    "return to home screen",
    "meeting ended",
    "you left the meeting",
    "has left the meeting",
    "has joined the meeting",
    "joined the meeting",
    "joining the meeting",
    "ask to join",
    "join now",
    # Additional Meet UI noise
    "ask gemini",
    "audio settings",
    "video settings",
    "turn off microphone",
    "turn on microphone",
    "available for this meeting",
    "keyboard_arrow",
    "open microphone",
    "close microphone",
    "hand raised",
    "raise hand",
    "lower hand",
    "background effects",
    "change background",
    "meeting code",
    "copy link",
    "add people",
    "message everyone",
    "you are muted",
    "unmute yourself",
    "noise cancellation",
    "this call is being",
    "everyone will see",
    "chat with everyone",
    # Google Meet meeting-code caption artefacts ("AM abc-def-ghi abc-def-ghi")
    "live captions have been turned",
    "captions have been turned",
    "current language is",
    # Meeting code line — starts with "AM " followed by the meet code repeated
    # Can't regex here so catch the repeated-code pattern via length+alpha heuristic
    # (handled in bot.py noise filter instead — see on_caption())
]

# ── JavaScript: extract all visible caption blocks from Meet DOM ───
#
# Google Meet renders captions in several possible container structures
# depending on the client version / feature flag:
#
#   Version A  (jsname spans — most specific):
#     <div>
#       <span class="...">Speaker Name</span>
#       <span jsname="YSxPC">growing caption text</span>
#     </div>
#
#   Version B  (aria-live="polite"):
#     <div aria-live="polite">Speaker Name\ncaption text</div>
#
#   Version C  (aria-atomic):
#     <div aria-atomic="true|false">text</div>
#
#   Version D  (bottom-viewport overlay — last resort):
#     Any wide, short div in the bottom 30% of the viewport.
#
# The JS layer deduplicates by text, preferring a named speaker over
# "Unknown", so the Python layer never receives two entries for the
# same text with different attribution.
#
_CAPTION_JS = r"""
() => {
    const NOISE = __NOISE__;

    const isNoise = s => {
        const lc = (s || '').toLowerCase();
        return NOISE.some(n => lc.includes(n));
    };

    // Collect results keyed by text.
    // Priority: real speaker name > "Unknown"
    const byText = {};

    function add(speaker, text) {
        speaker = (speaker || '').trim() || 'Unknown';
        text    = (text    || '').trim();
        if (!text || text.length < 3 || isNoise(text)) return;
        // Reject name-card artefacts where caption text IS the speaker name
        if (speaker !== 'Unknown' &&
            text.toLowerCase() === speaker.toLowerCase()) return;
        const existing = byText[text];
        if (!existing || existing.speaker === 'Unknown') {
            byText[text] = { speaker, text };
        }
    }

    // ── Strategy 1: jsname caption spans ─────────────────────────
    // These are Google Meet's internal caption text nodes.
    const captionJsnames = ['YSxPC', 'tgaKEf'];
    for (const jn of captionJsnames) {
        for (const el of document.querySelectorAll(`[jsname="${jn}"]`)) {
            const t = (el.innerText || '').trim();
            if (!t) continue;
            // Look for a speaker name in a sibling / ancestor span
            let speaker = 'Unknown';
            const container = el.closest('div');
            if (container) {
                for (const s of container.querySelectorAll('span, div')) {
                    if (s === el || s.contains(el)) continue;
                    const st = (s.innerText || '').trim();
                    if (st && st.length > 0 && st.length < 60 && !isNoise(st)) {
                        speaker = st;
                        break;
                    }
                }
            }
            add(speaker, t);
        }
    }

    // ── Strategy 2: aria-live="polite" ───────────────────────────
    for (const el of document.querySelectorAll('[aria-live="polite"]')) {
        const t = (el.innerText || '').trim();
        if (!t || isNoise(t)) continue;
        const lines = t.split('\n').map(l => l.trim()).filter(Boolean);
        if (lines.length >= 2) {
            add(lines[0], lines.slice(1).join(' '));
        } else if (lines.length === 1 && lines[0].length > 5) {
            add('Unknown', lines[0]);
        }
    }

    // ── Strategy 3: aria-atomic ───────────────────────────────────
    for (const attr of ['true', 'false']) {
        for (const el of document.querySelectorAll(`[aria-atomic="${attr}"]`)) {
            const t = (el.innerText || '').trim();
            if (!t || isNoise(t)) continue;
            const lines = t.split('\n').map(l => l.trim()).filter(Boolean);
            if (lines.length >= 2) {
                add(lines[0], lines.slice(1).join(' '));
            } else if (lines.length === 1 && lines[0].length > 5) {
                add('Unknown', lines[0]);
            }
        }
    }

    // ── Strategy 4b: any element with role="log" or aria-live (broader) ──
    // Newer Meet versions may use different jsname values. Cast a wider net.
    for (const el of document.querySelectorAll('[role="log"], [aria-live="assertive"]')) {
        const t = (el.innerText || '').trim();
        if (!t || isNoise(t)) continue;
        const lines = t.split('\n').map(l => l.trim()).filter(Boolean);
        if (lines.length >= 2) {
            add(lines[0], lines.slice(1).join(' '));
        } else if (lines.length === 1 && lines[0].length > 8) {
            add('Unknown', lines[0]);
        }
    }

    // ── Strategy 5: bottom-viewport caption overlay (fallback) ───
    const vh = window.innerHeight;
    for (const el of document.querySelectorAll('div[class]')) {
        const b = el.getBoundingClientRect();
        if (b.top < vh * 0.70 || b.width < 200 || b.height < 18 || b.height > 200) continue;
        const t = (el.innerText || '').trim();
        if (t.length < 10 || t.length > 500 || isNoise(t)) continue;
        const lines = t.split('\n').map(l => l.trim()).filter(Boolean);
        if (lines.length >= 2) {
            add(lines[0], lines.slice(1).join(' '));
        } else {
            add('Unknown', lines[0]);
        }
    }

    return Object.values(byText);
}
""".replace("__NOISE__", json.dumps(_UI_NOISE))

# JavaScript that detects a "meeting has ended" banner.
_ENDED_JS = """
() => {
    const t = (document.body.innerText || '').toLowerCase();
    return t.includes('meeting ended')
        || t.includes('you left the meeting')
        || t.includes('return to home screen');
}
"""

# JavaScript injected once after captions are enabled.
# Attaches a MutationObserver to the caption container so the Python
# on_speech_start callback fires in ~2 ms when the DOM changes —
# compared to 0–500 ms with polling.  Used ONLY for the interrupt
# signal; the normal polling loop still drives caption stabilisation.
#
# Replaces __FN_NAME__ with the page.expose_function name at runtime.
_MUTATION_OBSERVER_JS = r"""
(fnName) => {
    const CAPTION_JSNAMES = ['YSxPC', 'tgaKEf'];
    const NOISE = __NOISE__;

    const isNoise = s => {
        const lc = (s || '').toLowerCase();
        return NOISE.some(n => lc.includes(n));
    };

    // Mirror _CAPTION_JS Strategy 1: jsname spans only.
    // We only need speaker+text for the interrupt signal — we don't
    // need to cover every fallback strategy here.
    function extractCaptions() {
        const byText = {};
        function add(speaker, text) {
            speaker = (speaker || '').trim() || 'Unknown';
            text    = (text    || '').trim();
            if (!text || text.length < 3 || isNoise(text)) return;
            if (speaker !== 'Unknown' &&
                text.toLowerCase() === speaker.toLowerCase()) return;
            if (!byText[text] || byText[text].speaker === 'Unknown') {
                byText[text] = { speaker, text };
            }
        }
        for (const jn of CAPTION_JSNAMES) {
            for (const el of document.querySelectorAll('[jsname="' + jn + '"]')) {
                const t = (el.innerText || '').trim();
                if (!t) continue;
                let speaker = 'Unknown';
                const container = el.closest('div');
                if (container) {
                    for (const s of container.querySelectorAll('span, div')) {
                        if (s === el || s.contains(el)) continue;
                        const st = (s.innerText || '').trim();
                        if (st && st.length > 0 && st.length < 60 && !isNoise(st)) {
                            speaker = st;
                            break;
                        }
                    }
                }
                add(speaker, t);
            }
        }
        return Object.values(byText);
    }

    // Per-speaker JS-side debounce: max one Python call per 150 ms.
    // Prevents event storm through the Playwright bridge when Meet
    // updates the DOM word-by-word (~10–20 mutations per sentence).
    const lastCall = {};
    let observer   = null;

    function handleMutations() {
        const now = Date.now();
        for (const { speaker, text } of extractCaptions()) {
            if (now - (lastCall[speaker] || 0) < 150) continue;
            lastCall[speaker] = now;
            try { window[fnName]({ speaker, text }); } catch (_) {}
        }
    }

    function attach() {
        // Find the nearest stable ancestor of the caption text nodes.
        // Watching the body works but is noisy; a tighter target is better.
        let target = document.body;
        for (const jn of ['tgaKEf', 'YSxPC', 'BjGdaf']) {
            const el = document.querySelector('[jsname="' + jn + '"]');
            if (el) {
                target = el.closest('div[jsname]') || el.parentElement || document.body;
                break;
            }
        }
        if (observer) observer.disconnect();
        observer = new MutationObserver(handleMutations);
        observer.observe(target, { childList: true, subtree: true, characterData: true });
    }

    attach();

    // Exposed for the Python watchdog — re-attaches if Meet recreates the DOM.
    window.__caption_observer_reattach__ = attach;
}
""".replace("__NOISE__", json.dumps(_UI_NOISE))


# ══════════════════════════════════════════════════════════════════
class CaptionScraper:
    """
    Scrapes live Google Meet captions via DOM inspection only.

    Responsibilities:
      • Poll the Meet DOM every POLL_INTERVAL seconds.
      • Buffer each speaker's growing caption text.
      • Commit (save) a caption only after it has been stable for
        STABILIZATION_SEC seconds — this discards all intermediate
        word-by-word fragments and stores only complete sentences.
      • Replace "Unknown" speaker attributions with real names when
        Meet's name widget arrives late.
      • Hash-deduplicate all committed captions to prevent any row
        appearing twice regardless of which DOM selector found it.
    """

    def __init__(self, page):
        self.page       = page
        self.meet_frame = None      # Playwright Frame containing the Meet UI

        # ── Per-speaker rolling buffer ─────────────────────────────
        # { speaker_name: {
        #     "current_text":     str   — latest text seen for this speaker
        #     "last_change_time": float — monotonic time of last update
        #     "last_saved_text":  str   — last text we committed to disk/DB
        #     "pending_commit":   str   — text queued for immediate flush
        # }}
        self.speaker_buffers = {}

        # ── Pending Unknown captions ───────────────────────────────
        # { text_hash: (speaker, text, queued_at) }
        # Held until a named version of the same text arrives, or until
        # 2× the stabilization window expires (fallback commit).
        self.pending_unknown = {}

        # ── Duplicate prevention ───────────────────────────────────
        # { hash: commit_monotonic_time } — TTL-based dedup.
        # Entries expire after HASH_DEDUP_WINDOW_SEC so candidates can repeat
        # an identical sentence after a pause and still get a response.
        self.saved_hashes: dict = {}

        self.stabilization_delay = STABILIZATION_SEC
        self.captured   = 0
        self.silent_sec = 0.0
        self._file      = None      # Path to the local transcript file
        # ── In-memory transcript for live line-update support ─────
        # Each entry: [ts_short, speaker, text]
        # When a speaker's new text extends their last line, we update
        # that entry in-place and rewrite the file — no duplicate rows.
        self._transcript_lines  = []   # list of [ts_short, speaker, text]
        self._last_speaker_idx  = {}   # {speaker: index into _transcript_lines}
    # ── Internal helpers ──────────────────────────────────────────

    @staticmethod
    def _text_hash(text):
        """Stable SHA-1 hash of normalised text — used for dedup."""
        return hashlib.sha1(text.strip().lower().encode()).hexdigest()

    @staticmethod
    def _now():
        return time.monotonic()

    @staticmethod
    def _normalize(s):
        """Lowercase, strip punctuation and collapse whitespace."""
        s = s.lower()
        s = re.sub(r"[^\w\s]", "", s)
        return re.sub(r"\s+", " ", s).strip()

    def _has_saved_hash(self, h: str) -> bool:
        """Return True only if hash was committed within HASH_DEDUP_WINDOW_SEC."""
        t = self.saved_hashes.get(h)
        if t is None:
            return False
        if self._now() - t > HASH_DEDUP_WINDOW_SEC:
            del self.saved_hashes[h]
            return False
        return True

    def _add_saved_hash(self, h: str) -> None:
        """Record hash with current commit time (TTL-based)."""
        self.saved_hashes[h] = self._now()

    @classmethod
    def _is_continuation(cls, prev, new_text):
        """
        Return True if new_text is a fuzzy extension of prev.

        Google Meet speech recognition often revises punctuation and
        capitalisation as recognition improves, so a strict startswith
        check fails even when the sentence is merely growing longer.

        Strategy:
          1. If new_text is not longer than prev → not a continuation.
          2. Normalize both (lowercase, strip punctuation).
          3. If the normalized new text starts with the normalized prev → True.
          4. Allow up to 15 % character difference in the prefix portion
             to account for mid-word punctuation revisions.
        """
        if len(new_text) <= len(prev):
            return False
        np = cls._normalize(prev)
        nn = cls._normalize(new_text)
        if not np:
            return True           # prev was whitespace / punctuation only
        if nn.startswith(np):
            return True
        # Fuzzy: compare only the prefix slice of nn that is as long as np
        prefix = nn[:len(np)]
        diffs  = sum(1 for a, b in zip(np, prefix) if a != b)
        return diffs <= max(2, int(len(np) * 0.15))

    # ── Frame detection ───────────────────────────────────────────

    async def _detect_frame(self):
        msg = "🔍 Scanning frames for Google Meet..."
        print(msg); await push_log(msg)
        for frame in self.page.frames:
            try:
                if "meet.google.com" in frame.url:
                    msg = f"🖼️  Meet frame found: {frame.url[:80]}"
                    print(msg); await push_log(msg)
                    self.meet_frame = frame
                    return
            except Exception:
                continue
        msg = "⚠️  No Meet iframe detected – using main page"
        print(msg); await push_log(msg)
        self.meet_frame = self.page

    @property
    def _ctx(self):
        """The frame/page used for all DOM queries."""
        return self.meet_frame or self.page

    # ── Caption toggle ────────────────────────────────────────────

    async def _captions_already_on(self):
        """
        Return True ONLY if Google Meet is currently showing captions.

        Reliable signals:
          • The toolbar shows a "Turn off captions" button (aria-label contains
            "Turn off" AND "caption") — this button only appears when captions
            are active.
          • jsname="YSxPC" or jsname="tgaKEf" spans exist AND have non-empty
            innerText — these are Meet's live caption text nodes.

        Intentionally NOT used:
          • aria-live="polite" — this attribute exists on many Meet UI elements
            regardless of whether captions are on, so it gives false positives.
        """
        try:
            # Signal 1: "Turn off captions" button visible in toolbar
            for sel in [
                'button[aria-label*="Turn off captions" i]',
                'button[aria-label*="captions off" i]',
            ]:
                loc = self._ctx.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return True
            # Signal 2: jsname caption container nodes — presence alone is enough.
            # Right after enabling, nobody may be speaking yet but the container
            # exists in the DOM. Requiring text caused a false-negative every time.
            for jn in ['YSxPC', 'tgaKEf', 'BjGdaf', 'Qrt5E']:
                nodes = self._ctx.locator(f'[jsname="{jn}"]')
                if await nodes.count() > 0:
                    return True
            # Signal 3: newer Meet versions use a labelled region
            for sel in [
                '[aria-label*="Captions" i][role="region"]',
                '[aria-label*="Live caption" i]',
            ]:
                try:
                    loc = self._ctx.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    async def _enable_captions(self):
        msg = "📝 Enabling captions..."
        print(msg); await push_log(msg)

        # ── Already on? ───────────────────────────────────────────
        if await self._captions_already_on():
            msg = "   ✅ Captions already on"
            print(msg); await push_log(msg)
            return True

        _shortcut_sent = False  # Only send 'c' once — it's a toggle, not idempotent

        for attempt in range(1, ENABLE_RETRIES + 1):
            msg = f"   🔄 Caption enable attempt {attempt}/{ENABLE_RETRIES}..."
            print(msg); await push_log(msg)

            # ── Strategy 1: direct CC / caption button in toolbar ─
            direct_selectors = [
                'button[aria-label="Turn on captions (c)"]',
                'button[aria-label="Turn on captions"]',
                'button[aria-label*="captions" i]',
                'button[aria-label*="subtitles" i]',
                '[data-tooltip*="captions" i]',
                '[jsname="r8qRAd"]',
            ]
            try:
                for sel in direct_selectors:
                    btn = self._ctx.locator(sel)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        aria = (await btn.first.get_attribute("aria-label") or "").lower()
                        if "turn off" in aria or "off" in aria:
                            msg = "   ✅ Captions already on (direct button)"
                            print(msg); await push_log(msg)
                            return True
                        await btn.first.click()
                        await asyncio.sleep(1.5)
                        msg = f"   ✅ Captions enabled via direct button"
                        print(msg); await push_log(msg)
                        if await self._captions_already_on():
                            return True
            except Exception as e:
                msg = f"   ⚠️  Direct button error: {e}"
                print(msg); await push_log(msg)

            # ── Strategy 2: More options (⋮) menu ─────────────────
            # Captions is often buried inside the 3-dot overflow menu
            try:
                more_selectors = [
                    'button[aria-label="More options"]',
                    'button[aria-label*="more" i]',
                    '[data-tooltip*="More options" i]',
                    '[jsname="NakZHc"]',
                ]
                opened_menu = False
                for sel in more_selectors:
                    more_btn = self.page.locator(sel)
                    if await more_btn.count() > 0 and await more_btn.first.is_visible():
                        await more_btn.first.click()
                        await asyncio.sleep(1)
                        opened_menu = True
                        msg = "   📂 More-options menu opened"
                        print(msg); await push_log(msg)
                        break

                if opened_menu:
                    # Look for a captions menu item inside the popup
                    menu_item_selectors = [
                        'li[aria-label*="captions" i]',
                        'li[aria-label*="subtitles" i]',
                        '[role="menuitem"]:has-text("captions")',
                        '[role="menuitem"]:has-text("Captions")',
                        '[role="menuitem"]:has-text("Turn on captions")',
                        'span:has-text("Turn on captions")',
                        'div:has-text("Turn on captions")',
                    ]
                    for sel in menu_item_selectors:
                        item = self.page.locator(sel)
                        if await item.count() > 0 and await item.first.is_visible():
                            await item.first.click()
                            await asyncio.sleep(1.5)
                            msg = "   ✅ Captions enabled via More-options menu"
                            print(msg); await push_log(msg)
                            if await self._captions_already_on():
                                return True
                            break
                    else:
                        # Close menu with Escape if nothing matched
                        await self.page.keyboard.press("Escape")
                        await asyncio.sleep(0.5)
            except Exception as e:
                msg = f"   ⚠️  More-options menu error: {e}"
                print(msg); await push_log(msg)

            # ── Strategy 3: Activities panel ──────────────────────
            try:
                activities_selectors = [
                    'button[aria-label*="Activities" i]',
                    'button[aria-label*="activity" i]',
                    '[jsname="A5il2e"]',
                ]
                for sel in activities_selectors:
                    act_btn = self.page.locator(sel)
                    if await act_btn.count() > 0 and await act_btn.first.is_visible():
                        await act_btn.first.click()
                        await asyncio.sleep(1)
                        # Now look for captions option inside the panel
                        panel_selectors = [
                            '[aria-label*="captions" i]',
                            'div:has-text("Captions")',
                            'button:has-text("Captions")',
                        ]
                        for psel in panel_selectors:
                            pitem = self.page.locator(psel)
                            if await pitem.count() > 0 and await pitem.first.is_visible():
                                await pitem.first.click()
                                await asyncio.sleep(1.5)
                                msg = "   ✅ Captions enabled via Activities panel"
                                print(msg); await push_log(msg)
                                await self.page.keyboard.press("Escape")
                                if await self._captions_already_on():
                                    return True
                                break
                        else:
                            await self.page.keyboard.press("Escape")
                            await asyncio.sleep(0.5)
                        break
            except Exception as e:
                msg = f"   ⚠️  Activities panel error: {e}"
                print(msg); await push_log(msg)

            # ── Strategy 4: keyboard shortcut 'c' ─────────────────
            # Must click the page body first to ensure focus is on Meet.
            # Only sent ONCE — 'c' is a toggle; pressing it a second time
            # would turn captions back off.
            if not _shortcut_sent:
                try:
                    await self.page.mouse.click(400, 300)
                    await asyncio.sleep(0.3)
                    await self.page.keyboard.press("c")
                    _shortcut_sent = True
                    await asyncio.sleep(2.5)  # give Meet time to enable captions
                    msg = f"   ⌨️  Shortcut 'c' sent"
                    print(msg); await push_log(msg)
                    if await self._captions_already_on():
                        msg = "   ✅ Captions confirmed on after shortcut"
                        print(msg); await push_log(msg)
                        return True
                except Exception as e:
                    msg = f"   ⚠️  Keyboard shortcut error: {e}"
                    print(msg); await push_log(msg)

            await asyncio.sleep(2)

        msg = "   ⚠️  Could not confirm captions on — proceeding anyway"
        print(msg); await push_log(msg)
        return False

    # ── Meeting-end detection ─────────────────────────────────────

    async def _is_over(self):
        if self.page.is_closed():
            return True
        try:
            if "meet.google.com" not in self.page.url:
                return True
        except Exception:
            return True
        try:
            if await self._ctx.evaluate(_ENDED_JS):
                return True
        except Exception:
            pass
        return False

    # ── Local file helpers ────────────────────────────────────────

    def _init_file(self, session_id):
        TRANSCRIPT_DIR.mkdir(exist_ok=True)
        self._file = TRANSCRIPT_DIR / f"{session_id}.txt"
        return self._file

    def _write_line(self, ts_short, speaker, text):
        """Append a brand-new line (used only by _rewrite_file internally)."""
        if not self._file:
            return
        with open(self._file, "a", encoding="utf-8") as f:
            f.write(f"[{ts_short}] {speaker}: {text}\n")

    def _rewrite_file(self):
        """Rewrite the entire transcript file from the in-memory line list."""
        if not self._file:
            return
        with open(self._file, "w", encoding="utf-8") as f:
            for ts_short, speaker, text in self._transcript_lines:
                f.write(f"[{ts_short}] {speaker}: {text}\n")

    def _find_transcript_continuation(self, text):
        """
        Scan the last 10 committed lines for one whose text is a fuzzy
        prefix of `text` (meaning `text` extends it).
        Returns (index, existing_text) or (None, None).
        """
        LOOKBACK = 10
        start = max(0, len(self._transcript_lines) - LOOKBACK)
        for idx in range(len(self._transcript_lines) - 1, start - 1, -1):
            _, _, existing_text = self._transcript_lines[idx]
            if self._is_continuation(existing_text, text):
                return idx, existing_text
        return None, None

    def _record_line(self, ts_short, speaker, text):
        """
        Add or update a transcript line for this speaker.

        If the speaker's most recent line is a prefix of `text`
        (i.e. this is just the same sentence growing longer), the
        existing entry is updated in-place and the file is rewritten.
        Otherwise a new line is appended.

        Returns True if the file was rewritten (update), False if appended.
        """
        last_idx = self._last_speaker_idx.get(speaker)
        if last_idx is not None:
            prev_ts, prev_sp, prev_txt = self._transcript_lines[last_idx]
            if self._is_continuation(prev_txt, text):
                # Extend the existing line in-place (keep original timestamp)
                self._transcript_lines[last_idx] = [prev_ts, speaker, text]
                self._rewrite_file()
                return True  # updated in-place

        # New entry
        self._transcript_lines.append([ts_short, speaker, text])
        self._last_speaker_idx[speaker] = len(self._transcript_lines) - 1
        if self._file:
            with open(self._file, "a", encoding="utf-8") as f:
                f.write(f"[{ts_short}] {speaker}: {text}\n")
        return False  # appended

    # ── DOM polling ───────────────────────────────────────────────

    async def _poll_dom(self):
        """
        Run the caption JavaScript in the Meet frame.
        Returns a list of (speaker, text) tuples, already deduplicated
        by the JS layer (real name preferred over Unknown).
        """
        try:
            results = await self._ctx.evaluate(_CAPTION_JS)
        except Exception:
            return []
        if not results:
            return []
        out = []
        for item in results:
            speaker = (item.get("speaker") or "Unknown").strip()
            text    = (item.get("text") or "").strip()
            if text and len(text) >= 3:
                out.append((speaker, text))
        return out

    # ── Stabilization buffer logic ────────────────────────────────

    def _update_buffer(self, speaker, text):
        """
        Feed a (speaker, text) pair from the DOM into the rolling buffer.

        Case handling:
          1. text == prev          → no change, ignore
          2. len(text) < len(prev) → Meet rolled/reset the DOM node, ignore
          3. text.startswith(prev) → sentence growing word by word;
                                     update buffer, DO NOT save yet
          4. Unrelated new text   → old sentence is complete; queue it
                                     for immediate flush, start fresh buffer
        """
        buf = self.speaker_buffers.get(speaker)

        if buf is None:
            # First time seeing this speaker — open a fresh buffer
            self.speaker_buffers[speaker] = {
                "current_text":     text,
                "last_change_time": self._now(),
                "last_saved_text":  "",
            }
            return

        prev = buf["current_text"]

        # Case 1: nothing changed
        if text == prev:
            # After SPEAKER_RESET_SEC of inactivity, clear last_saved_text so the
            # same sentence can be re-committed (handles candidate repeating a question).
            if self._now() - buf.get("last_change_time", self._now()) > SPEAKER_RESET_SEC:
                buf["last_saved_text"] = ""
            return

        # Case 2: shorter → transient UI glitch or rolling reset, ignore
        if len(text) < len(prev):
            return

        # Case 3: incremental growth of the current sentence
        if prev == "" or self._is_continuation(prev, text):
            buf["current_text"]     = text
            buf["last_change_time"] = self._now()
            return

        # Case 4: new unrelated sentence — the previous one is complete.
        # Queue it for immediate commit, then start a fresh buffer entry.
        if prev and prev != buf.get("last_saved_text", ""):
            buf["pending_commit"] = prev

        buf["current_text"]     = text
        buf["last_change_time"] = self._now()

    # ── Stabilization flush ───────────────────────────────────────

    async def _flush_stable(self, on_caption):
        """
        Walk all speaker buffers. Commit captions that are ready:
          • Immediately: any entry queued via pending_commit
                         (detected sentence break in _update_buffer)
          • After delay:  any entry unchanged for >= stabilization_delay

        Also handles expiry of Unknown pending entries.
        Returns the number of captions committed in this pass.
        """
        committed = 0
        now = self._now()

        for speaker, buf in list(self.speaker_buffers.items()):
            # Immediate flush for completed sentences (case 4 transition)
            pending = buf.pop("pending_commit", None)
            if pending and pending != buf.get("last_saved_text", ""):
                committed += await self._commit_caption(speaker, pending, buf, on_caption)

            # Normal stabilization: text unchanged for >= delay
            text = buf["current_text"]
            if not text or text == buf.get("last_saved_text", ""):
                continue
            elapsed = now - buf["last_change_time"]
            if elapsed >= self.stabilization_delay:
                committed += await self._commit_caption(speaker, text, buf, on_caption)

        # Expire Unknown pending entries that never received a real name
        for h, (spk, txt, queued_at) in list(self.pending_unknown.items()):
            if (now - queued_at) >= self.stabilization_delay * 2:
                del self.pending_unknown[h]
                committed += await self._do_write(spk, txt, on_caption)

        return committed

    # ── Caption commit decision ───────────────────────────────────

    async def _commit_caption(self, speaker, text, buf, on_caption):
        """
        Decide whether to write this (speaker, text) pair.

        Unknown replacement logic:
          When speaker == "Unknown":
            • Store in pending_unknown keyed by text hash.
            • Do NOT write to disk/DB yet.
            • If a named version of the same text arrives, the pending
              Unknown entry is silently discarded; only the named version
              is written.
            • If no named version arrives within 2× the stabilization
              window, the Unknown entry is committed as a fallback
              (handled in _flush_stable).

          When speaker is a real name:
            • Cancel any pending Unknown entry for the same text hash.
            • If text hash already in saved_hashes → skip (already saved).
            • Otherwise write immediately.
        """
        text = text.strip()
        if not text:
            return 0

        h = self._text_hash(text)

        if speaker == "Unknown":
            # If this text extends an already-committed named line,
            # update that line in-place rather than adding a duplicate row.
            ext_idx, ext_txt = self._find_transcript_continuation(text)
            if ext_idx is not None:
                prev_ts, prev_sp, _ = self._transcript_lines[ext_idx]
                self._transcript_lines[ext_idx] = [prev_ts, prev_sp, text]
                self._rewrite_file()
                self._add_saved_hash(h)
                buf["last_saved_text"] = text
                return 0
            # If already saved or already pending, skip
            if self._has_saved_hash(h) or h in self.pending_unknown:
                buf["last_saved_text"] = text
                return 0
            # Park it — a real name may arrive shortly
            self.pending_unknown[h] = (speaker, text, self._now())
            buf["last_saved_text"] = text
            return 0

        # Real speaker: cancel any pending Unknown for the same text (exact)
        if h in self.pending_unknown:
            del self.pending_unknown[h]
        # Also cancel any pending Unknown whose text fuzzy-overlaps this text
        for uh, (uspk, utxt, _utime) in list(self.pending_unknown.items()):
            if self._is_continuation(utxt, text) or self._is_continuation(text, utxt):
                del self.pending_unknown[uh]

        # Already committed under any speaker name
        if self._has_saved_hash(h):
            buf["last_saved_text"] = text
            return 0

        buf["last_saved_text"] = text
        return await self._do_write(speaker, text, on_caption)

    # ── Actual persistence ────────────────────────────────────────

    async def _do_write(self, speaker, text, on_caption):
        """
        Persist one stabilized, deduplicated caption:
          1. Format:  [HH:MM:SS] Speaker: Text
          2. Append to  transcription_recordings/{session_id}.txt
          3. Call async on_caption(doc) callback → MongoDB
          4. Register hash to prevent future duplicates
        """
        h = self._text_hash(text)
        if self._has_saved_hash(h):
            return 0

        # If the new text extends an already-committed line, update in-place.
        ext_idx, _ = self._find_transcript_continuation(text)
        if ext_idx is not None:
            prev_ts, prev_sp, _ = self._transcript_lines[ext_idx]
            # Keep the original speaker name (prefer named over Unknown)
            keep_sp = prev_sp if prev_sp != "Unknown" else speaker
            self._transcript_lines[ext_idx] = [prev_ts, keep_sp, text]
            self._rewrite_file()
            self._add_saved_hash(h)
            msg = f"📄 Updated line (extended) [{keep_sp}]: {text[:70]}"
            print(msg); await push_log(msg)
            return 0

        self._add_saved_hash(h)

        self.captured   += 1
        self.silent_sec  = 0

        ts_iso   = datetime.utcnow().isoformat()
        ts_short = ts_iso[11:19]    # HH:MM:SS

        # Write to local transcript file
        try:
            updated = self._record_line(ts_short, speaker, text)
            action  = "Updated" if updated else "Saved"
            msg = f"📄 {action} caption #{self.captured}"
            print(msg); await push_log(msg)
        except Exception as e:
            msg = f"⚠️  File write error: {e}"
            print(msg); await push_log(msg)

        # MongoDB callback (session_id injected by scrape_loop wrapper)
        if on_caption:
            doc = {
                "session_id": None,
                "timestamp":  ts_iso,
                "speaker":    speaker,
                "text":       text,
            }
            try:
                await on_caption(doc)
            except Exception as e:
                msg = f"⚠️  Callback error: {e}"
                print(msg); await push_log(msg)

        msg = f"✅ #{self.captured} [{ts_short}] {speaker}: {text[:70]}"
        print(msg); await push_log(msg)
        return 1

    # ── MutationObserver injection ────────────────────────────────

    async def _inject_mutation_observer(self, fn_name: str, callback) -> bool:
        """
        Expose `callback` as window[fn_name] then inject the MutationObserver.

        Returns True on success, False if injection failed (polling still works).
        The observer fires callback({speaker, text}) within ~2 ms of any DOM
        caption change — used exclusively for the TTS interrupt signal.
        """
        try:
            # page.expose_function makes fn_name available in ALL frames.
            # Wrap in try/except — re-injection after watchdog re-attach
            # doesn't need to re-expose (name is already bound).
            try:
                await self.page.expose_function(fn_name, callback)
            except Exception:
                pass  # Already exposed from a previous inject call

            # Evaluate in the correct frame context (main page or Meet iframe)
            await self._ctx.evaluate(f"({_MUTATION_OBSERVER_JS})('{fn_name}')")
            msg = "MutationObserver injected — interrupt detection ~2 ms"
            print(msg); await push_log(msg)
            return True
        except Exception as e:
            msg = f"MutationObserver inject failed (polling still active): {e}"
            print(msg); await push_log(msg)
            return False

    async def _observer_watchdog(self, fn_name: str) -> None:
        """
        Every 5 s, re-attach the MutationObserver in case Google Meet
        recreated its caption DOM subtree (page events, reconnects, etc.).
        If __caption_observer_reattach__ is no longer on window the full
        JS block is re-injected (but expose_function is skipped — already bound).
        """
        while True:
            await asyncio.sleep(5)
            try:
                reattach_exists = await self._ctx.evaluate(
                    "() => typeof window.__caption_observer_reattach__ === 'function'"
                )
                if reattach_exists:
                    await self._ctx.evaluate(
                        "() => window.__caption_observer_reattach__()"
                    )
                else:
                    # Full re-inject (page navigation reset window)
                    await self._ctx.evaluate(
                        f"({_MUTATION_OBSERVER_JS})('{fn_name}')"
                    )
                    msg = "MutationObserver re-injected by watchdog (window reset)"
                    print(msg); await push_log(msg)
            except asyncio.CancelledError:
                return
            except Exception:
                pass  # Non-fatal — polling loop is still running

    # ── Main scrape loop ──────────────────────────────────────────

    async def scrape_loop(self, session_id, on_caption=None, on_speech_start=None):
        """
        Poll for captions until the meeting ends.

        For every stabilized, deduplicated caption:
          1. Appends  transcription_recordings/{session_id}.txt
          2. Calls    await on_caption(doc)  → MongoDB persistence

        Args:
            session_id:     unique session identifier (e.g. "20260224_144700")
            on_caption:     async callback receiving:
                            { session_id, timestamp, speaker, text }
            on_speech_start: optional async callback fired immediately when a
                            speaker's DOM text changes (before stabilization).
                            Receives { speaker, text }.  Used for early interrupt.
        """
        # Thin wrapper: inject session_id before hitting the callback
        _upstream = on_caption
        async def _wrapped(doc):
            doc["session_id"] = session_id
            if _upstream:
                await _upstream(doc)

        await self._detect_frame()
        path = self._init_file(session_id)

        msg = f"🎬 Caption loop started — polling every {POLL_INTERVAL}s"
        print(msg); await push_log(msg)
        msg = f"📝 Transcript file: {path}"
        print(msg); await push_log(msg)

        await self._enable_captions()

        # ── MutationObserver (fast interrupt path) ─────────────────
        # Inject AFTER captions are enabled so the caption container
        # exists in the DOM.  Falls back gracefully if injection fails
        # — the polling loop below is always the source of truth for
        # caption stabilisation and DB writes.
        _watchdog_task = None
        if on_speech_start:
            _fn_name = f"onSpeechStart_{session_id}"
            _injected = await self._inject_mutation_observer(_fn_name, on_speech_start)
            if _injected:
                _watchdog_task = asyncio.create_task(
                    self._observer_watchdog(_fn_name)
                )

        iteration = 0
        try:
            while True:
                iteration += 1

                if await self._is_over():
                    msg = "⏹️  Meeting ended (page closed / URL changed / ended banner)"
                    print(msg); await push_log(msg)
                    break

                # Step 1: pull all visible captions from the DOM
                raw = await self._poll_dom()

                # Step 2: feed each into the per-speaker stabilization buffer
                for speaker, text in raw:
                    self._update_buffer(speaker, text)
                # Note: on_speech_start is now fired by the MutationObserver
                # (injected above) which reacts in ~2 ms.  The polling fallback
                # path that called on_speech_start here is removed to avoid
                # double-firing when the observer is active.  If the observer
                # failed to inject, on_speech_start simply won't fire from the
                # polling path either — interrupt still works via on_caption.

                # Step 3: commit any buffers that have now stabilized
                new_saves = await self._flush_stable(_wrapped)

                if new_saves == 0:
                    self.silent_sec += POLL_INTERVAL

                # Periodic status line every 10 iterations
                if iteration % 10 == 0:
                    msg = (
                        f"⏱️  iter={iteration}  saved={self.captured}"
                        f"  silent={self.silent_sec:.0f}s"
                        f"  buffers={len(self.speaker_buffers)}"
                    )
                    print(msg); await push_log(msg)

                if self.silent_sec >= SILENT_WARN_SEC:
                    msg = f"⚠️  No new captions for {self.silent_sec:.0f}s — rechecking captions..."
                    print(msg); await push_log(msg)
                    self.silent_sec = 0
                    # Re-enable captions if they went off (Google Meet may have toggled them)
                    try:
                        if not await self._captions_already_on():
                            msg = "Captions not detected — re-enabling..."
                            print(msg); await push_log(msg)
                            await self._enable_captions()
                    except Exception:
                        pass

                await asyncio.sleep(POLL_INTERVAL)

        except Exception as e:
            msg = f"❌ Scrape loop crashed: {e}"
            print(msg); await push_log(msg)

        finally:
            # Stop the MutationObserver watchdog
            if _watchdog_task and not _watchdog_task.done():
                _watchdog_task.cancel()
                try:
                    await _watchdog_task
                except asyncio.CancelledError:
                    pass

            # End-of-meeting flush: commit any text still sitting in buffers
            msg = "🔄 Flushing remaining buffers..."
            print(msg); await push_log(msg)
            for speaker, buf in self.speaker_buffers.items():
                text = buf["current_text"]
                if text and text != buf.get("last_saved_text", ""):
                    await self._do_write(speaker, text, _wrapped)
            # Commit any Unknown entries still in pending
            for h, (spk, txt, _) in list(self.pending_unknown.items()):
                if not self._has_saved_hash(h):
                    await self._do_write(spk, txt, _wrapped)

            msg = f"✅ Scrape loop done — {self.captured} captions saved"
            print(msg); await push_log(msg)


# ── Public entry point ─────────────────────────────────────────────

async def scrape_meeting_captions(page, session_id, on_caption_callback=None, on_speech_start=None):
    """Create a CaptionScraper and run its scrape loop."""
    scraper = CaptionScraper(page)
    await scraper.scrape_loop(session_id, on_caption_callback, on_speech_start)

