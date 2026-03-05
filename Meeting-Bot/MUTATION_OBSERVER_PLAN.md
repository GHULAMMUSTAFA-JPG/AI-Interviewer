# MutationObserver Interrupt Plan

## Goal

Replace DOM polling for interrupt detection with a JavaScript MutationObserver
injected into the Playwright page. This eliminates the 0–500ms polling jitter on
the interrupt path, bringing interrupt detection latency from ~250ms average down
to ~2ms.

## What Changes vs What Stays the Same

### STAYS THE SAME (do not touch)
- Polling loop in `scrape_loop()` — still runs, still feeds caption stabilization
- `STABILIZATION_SEC` (1.0s) — unchanged
- `UTTERANCE_SILENCE_SEC` (1.2s) — unchanged
- `on_caption` callback — unchanged, still the path to DB save and LLM trigger
- All utterance buffer logic in `bot.py` — unchanged
- Echo filter — unchanged
- MongoDB insert path — unchanged
- LLM trigger timing — unchanged (still fires after full utterance flushes)

### CHANGES
- `caption_scraper.py` — inject MutationObserver JS after `_enable_captions()`
- `caption_scraper.py` — expose Python callback via `page.expose_function`
- `bot.py` — pass the exposed function name into `scrape_meeting_captions()`

The MutationObserver fires `on_speech_start` ONLY (interrupt signal).
It does NOT replace `on_caption`. It does NOT save to DB. It does NOT trigger LLM.

---

## Architecture: Two Paths Running in Parallel

```
Google Meet DOM
      │
      ├─── MutationObserver (JS, instant)
      │         │
      │         └─ on_speech_start callback (~2ms)
      │                   │
      │                   └─ interrupt signal → MongoDB tts_interrupt=True
      │                      (only if bot_speaking=True, debounced per utterance)
      │
      └─── Polling loop (every 0.5s, unchanged)
                │
                └─ stabilization buffer (1.0s)
                          │
                          └─ utterance buffer (1.2s)
                                    │
                                    └─ DB insert → LLM call
```

---

## Files to Change

### 1. `caption_scraper.py`

**New method: `_inject_mutation_observer(on_speech_start_fn_name)`**

Injects JavaScript into the Playwright page that:
1. Finds the caption container (same selectors as `_poll_dom`)
2. Attaches a MutationObserver to it
3. On each mutation, extracts the speaker + current text
4. JS-side debounce: only calls Python if text changed and >100ms since last call
   for that speaker (prevents event storm through Playwright bridge)
5. Calls `window[on_speech_start_fn_name]({speaker, text})`

**New method: `_start_observer_watchdog(on_speech_start_fn_name)`**

Background task that every 5s:
- Checks if the caption container still exists and observer is attached
- Re-injects the observer if not (handles Google Meet DOM recreation)

**Changes to `scrape_loop()`**

After `_enable_captions()`, if `on_speech_start` is provided:
1. Expose the Python callback via `page.expose_function(fn_name, on_speech_start)`
2. Call `_inject_mutation_observer(fn_name)`
3. Start the observer watchdog task
4. On loop exit, cancel the watchdog task

**`scrape_meeting_captions()` signature** — no change needed, already accepts
`on_speech_start`.

---

### 2. `bot.py`

No structural changes needed. `_on_speech_start` already exists and is already
passed to `scrape_meeting_captions`. The MutationObserver calls it directly.

The existing debounce (`_speech_start_interrupt_sent`) already prevents
double-sends, so the event storm from MutationObserver is already handled
at the Python level too.

---

## JavaScript MutationObserver Design

```javascript
(function(fnName) {
  // Selectors for the caption container — same as Python poll_dom
  const SELECTORS = [
    '[jsname="tgaKEf"]',           // primary Meet captions container
    '.a4cQT',
    '[data-message-text]',
  ];

  let container = null;
  let observer = null;
  // Per-speaker last-call time for JS-side debounce
  const lastCall = {};

  function findContainer() {
    for (const sel of SELECTORS) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function extractCaptions() {
    // Extract speaker + text from DOM — mirrors Python _poll_dom logic
    const results = [];
    // Speaker name spans
    const speakerEls = document.querySelectorAll('[data-speaker-id], .zs7s8d');
    // ... (mirrors existing JS caption extraction in caption_scraper.py)
    return results;
  }

  function attach() {
    container = findContainer();
    if (!container) return false;

    observer = new MutationObserver(() => {
      const captions = extractCaptions();
      const now = Date.now();
      for (const {speaker, text} of captions) {
        if (!text) continue;
        // JS-side debounce: max 1 call per speaker per 100ms
        if (now - (lastCall[speaker] || 0) < 100) continue;
        lastCall[speaker] = now;
        window[fnName]({speaker, text});
      }
    });

    observer.observe(container, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    return true;
  }

  // Initial attach
  attach();

  // Expose re-attach function for the Python watchdog
  window.__reattach_observer__ = attach;

})(FUNCTION_NAME_PLACEHOLDER);
```

---

## Observer Watchdog (Python)

```python
async def _start_observer_watchdog(page, fn_name):
    while True:
        await asyncio.sleep(5)
        try:
            still_attached = await page.evaluate(
                "() => typeof window.__reattach_observer__ === 'function'"
            )
            if not still_attached:
                await _inject_mutation_observer(page, fn_name)
        except Exception:
            pass
```

---

## Problems to Handle

| Problem | Solution |
|---|---|
| Event storm (10-20 JS callbacks per sentence) | JS-side 100ms debounce per speaker |
| Observer silently lost when Meet recreates DOM | Python watchdog re-attaches every 5s |
| Frame detection must complete before injection | Inject only after `_detect_frame()` + `_enable_captions()` |
| `page.expose_function` name collision on restart | Use unique name with session_id suffix |
| Playwright bridge latency (~2-5ms per call) | Acceptable — still 50x faster than polling |
| Observer attaches to wrong frame | Use `page.frame` context where captions live |

---

## New Latency Chain (after implementation)

```
Candidate speaks → Google Meet updates DOM
  → MutationObserver fires                     ~2ms
  → JS debounce passes (first call per 100ms)  ~0ms
  → Python on_speech_start callback            ~3ms (Playwright bridge)
  → MongoDB find_one + update_one              ~16ms
  → TTS change stream wakes up                 ~10ms
  → pacat.terminate()                          ~50ms
                                           ──────────
  Total interrupt latency:                    ~81ms

  Previous (polling):                     ~320ms average
  Improvement:                                ~4x
```

---

## What This Does NOT Fix

- LLM latency (~1.2s) — unrelated
- ElevenLabs TTFB (~0.68s) — unrelated
- The time from candidate FINISHING speech to LLM response — unchanged
  (still 1.0s stabilization + 1.2s utterance buffer minimum)
- Caption accuracy — still Google Meet's STT

---

## Testing Checklist

- [ ] Observer attaches on meeting join
- [ ] Interrupt fires within ~100ms of candidate starting to speak
- [ ] No double interrupt sends (debounce works)
- [ ] LLM is NOT called prematurely (only after full utterance)
- [ ] Polling loop still saves captions correctly
- [ ] Observer re-attaches after 5s watchdog cycle
- [ ] No event storm errors in logs
- [ ] Works after 10+ minutes (observer not lost)
- [ ] Fallback: if observer fails to attach, polling still works normally

---

## Rollback

Switch to `improvements/timing-and-status` branch — full polling-based
implementation, fully tested.

```bash
git checkout improvements/timing-and-status
docker compose up --build -d meeting-bot
```
