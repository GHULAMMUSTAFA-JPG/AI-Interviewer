"""
speech_injector.py — Web Speech API Injection Module (Streaming STT)
─────────────────────────────────────────────────────────────────────
Injects a webkitSpeechRecognition script into the active Google Meet
page so that meeting audio is transcribed natively by the browser.

Streaming events emitted via console.log for Python capture:
    STT_INTERIM:<text>                — real-time partial transcript
    STT_FINAL:<text> CONF:<confidence> — confirmed final transcript
    STT_SPEECH_START:                 — candidate started speaking
    STT_SPEECH_END:                   — candidate stopped speaking
    TRANSCRIPT_EVENT:<text>           — final transcript for Python capture

Window globals exposed:
    window.__speechActive      — true while recognition is running
    window.__lastTranscript    — most-recent interim transcript string
    window.__speechStopped     — set to true to prevent auto-restart
    window.__speechRecognition — the active SpeechRecognition instance
    window.__botSpeaking       — true while TTS is playing — STT paused
"""

from logger import push_log

# ── JavaScript injected into the Meet page ────────────────────────
#
# All communication back to Python happens via console.log ONLY.
# Python captures STT_INTERIM / STT_FINAL / STT_SPEECH_START / STT_SPEECH_END
# events from page.on("console").
# No fetch() / XHR is used — zero browser security restrictions.
#
# Placeholder (positional):
#   %s[0] — JSON-encoded session_id string  e.g. "20260306_120000"

_SPEECH_API_JS = r"""
(function(sessionId) {

    // ── Guard: prevent double-injection ───────────────────────
    if (window.__speechActive) {
        console.log('[BOT] Speech recognition already active — skipping re-inject');
        return;
    }

    const SpeechRecognition =
        window.SpeechRecognition || window.webkitSpeechRecognition;

    if (!SpeechRecognition) {
        console.error('[BOT] SpeechRecognition API not available in this context');
        window.__speechActive = false;
        return;
    }

    // ── State globals ──────────────────────────────────────────
    window.__speechActive       = true;
    window.__speechStopped      = false;
    window.__restartPending     = false;
    window.__lastTranscript     = '';
    window.__botSpeaking        = false;  // true while TTS is playing — STT paused

    // ── 2-second debounce state ────────────────────────────────
    // Problem: recognition.continuous=true fires a final event for each
    // short segment while the candidate is still speaking.  Each final
    // would fire TRANSCRIPT_EVENT → Gemini → agent interrupts the candidate.
    //
    // REAL-TIME FIX: Emit interim results IMMEDIATELY as they come in.
    // Don't wait for silence. Stream transcripts in real-time.
    // Only flush final results once to avoid duplicates.
    var __speechBuffer      = '';     // accumulates final text in one turn
    var __silenceTimer      = null;   // NOT USED - VAD handles silence now
    var __lastSentText      = '';     // dedup: don't send the same sentence twice
    var __lastInterimSent   = '';     // track last interim to avoid spam
    var __speechStartTime   = null;   // when speech started
    var __vadActive         = false;  // VAD currently detecting speech

    function _getSilenceMs() {
        var words = __speechBuffer.trim().split(/\s+/).filter(Boolean).length;
        if (words > 20) return 300;   // VAD-enhanced: faster for long answers
        if (words <= 10) return 200;  // VAD-enhanced: very fast for short
        return 250;                   // VAD-enhanced: balanced
    }

    // ── STT pause/resume — called by Python via page.evaluate() ───
    // __pauseSTT() fires when TTS starts; __resumeSTT() fires when TTS ends.
    // This prevents the bot from hearing its own voice as candidate input.
    window.__pauseSTT = function() {
        window.__botSpeaking = true;
        if (__silenceTimer !== null) { clearTimeout(__silenceTimer); __silenceTimer = null; }
        // Flush any buffered candidate speech BEFORE stopping (prevents losing
        // the candidate's answer if they were mid-sentence when TTS armed).
        if (__speechBuffer.trim()) {
            _flushBuffer();
        } else {
            __speechBuffer = '';
        }
        try { recognition.stop(); } catch(e) {}
        window.__speechActive = false;
        console.log('[STT] PAUSED — bot is speaking, STT suppressed');
    };

    window.__resumeSTT = function() {
        window.__botSpeaking = false;
        if (!window.__speechStopped && !window.__restartPending) {
            var _attempts = 0;
            function _tryStart() {
                try {
                    recognition.start();
                    window.__speechActive = true;
                    console.log('[STT] RESUMED — listening for candidate');
                } catch(e) {
                    _attempts++;
                    if (_attempts < 5) {
                        // Chrome briefly refuses rapid start/stop — retry with back-off
                        setTimeout(_tryStart, 300 * _attempts);
                    } else {
                        console.warn('[STT] __resumeSTT exhausted retries: ' + e.message);
                    }
                }
            }
            _tryStart();
        }
    };

    function _flushBuffer() {
        var text = __speechBuffer.trim();
        __speechBuffer = '';
        __silenceTimer = null;
        if (!text) return;
        if (text === __lastSentText) {
            console.log('[BOT] [STT] Duplicate suppressed: ' + text.substring(0, 60));
            return;
        }
        __lastSentText = text;
        console.log('[STT] Candidate: ' + text);
        console.log('STT_FINAL:' + text + ' CONF:1.00');
        // Backward compat: still emit TRANSCRIPT_EVENT for Python capture
        console.log('TRANSCRIPT_EVENT:' + text);
    }

    // ── Configure recognition ──────────────────────────────────
    const recognition           = new SpeechRecognition();
    recognition.continuous      = true;
    recognition.interimResults  = true;
    
    // Language configuration - supports multiple languages
    // Default: en-US, but can be overridden via window.STT_LANGUAGE
    // Common codes: 'en-US', 'en-GB', 'ur-PK', 'hi-IN', 'es-ES', 'fr-FR'
    recognition.lang            = window.STT_LANGUAGE || 'en-US';
    recognition.maxAlternatives = 3;  // Get multiple alternatives for better accuracy

    window.__speechRecognition  = recognition;

    // ── Event handlers ─────────────────────────────────────────
    recognition.onstart = function() {
        window.__speechActive = true;
        console.log('🎤 [BOT] Speech recognition active  session=' + sessionId);
    };

    recognition.onaudiostart = function() {
        console.log('🔊 [BOT] Audio capture started — microphone input received');
    };

    recognition.onspeechstart = function() {
        // Cancel any pending flush — candidate is still speaking
        if (__silenceTimer !== null) {
            clearTimeout(__silenceTimer);
            __silenceTimer = null;
            console.log('[BOT] [STT] Speech resumed — silence timer cancelled');
        }
        console.log('STT_SPEECH_START:');
    };

    recognition.onspeechend = function() {
        console.log('[BOT] Speech segment ended');
        console.log('STT_SPEECH_END:');
        // Start 2-second silence timer — emit buffered text after SILENCE_MS
        if (__silenceTimer !== null) {
            clearTimeout(__silenceTimer);
        }
        __silenceTimer = setTimeout(function() {
            _flushBuffer();
        }, _getSilenceMs());
    };

    recognition.onnomatch = function() {
        console.warn('[BOT] No speech match returned for this segment');
    };

    recognition.onresult = function(event) {
        var interimText = '';
        var finalText = '';

        for (var i = event.resultIndex; i < event.results.length; i++) {
            var text = event.results[i][0].transcript;
            var conf = event.results[i][0].confidence;

            if (event.results[i].isFinal) {
                // Accumulate final results
                finalText += (text + ' ');
                console.log('[BOT] Final segment: ' + text.trim());
            } else {
                // INTERIM results - emit IMMEDIATELY for real-time
                interimText += text;
                // CRITICAL: Emit EVERY interim update (word-by-word)
                console.log('STT_INTERIM:' + interimText.trim());
            }
        }

        window.__lastTranscript = interimText || __speechBuffer;

        // Buffer final results and emit once
        if (finalText.trim()) {
            __speechBuffer += finalText;
            console.log('[AGENT] Generating response');
            console.log('💬 [BOT] Buffered final: ' + finalText.trim());
        }
    };

    recognition.onerror = function(event) {
        console.error('❌ [BOT] SpeechRecognition error: ' + event.error +
                      '  (message: ' + (event.message || 'none') + ')');
        window.__speechActive = false;

        // Do NOT restart if bot is speaking — 'no-speech' errors are normal
        // when STT is intentionally capturing silence (bot's own TTS audio).
        if (!window.__speechStopped && !window.__botSpeaking) {
            window.__restartPending = true;
            setTimeout(function() {
                window.__restartPending = false;
                if (!window.__speechStopped && !window.__botSpeaking) {
                    try {
                        recognition.start();
                        window.__speechActive = true;
                        console.log('[BOT] Restarted after error: ' + event.error);
                    } catch(e) {
                        console.warn('[BOT] Restart after error failed: ' + e.message);
                    }
                }
            }, 2000);
        }
    };

    recognition.onend = function() {
        console.log('⚠️  [BOT] SpeechRecognition ended — will restart');
        window.__speechActive = false;

        // Flush any buffered text before restarting (e.g. network reset mid-speech)
        if (__speechBuffer.trim() && __silenceTimer === null) {
            _flushBuffer();
        }

        if (window.__restartPending) {
            console.log('[BOT] Restart already pending — onend skipping');
            return;
        }

        if (!window.__speechStopped && !window.__botSpeaking) {
            try {
                recognition.start();
                window.__speechActive = true;
                console.log('[BOT] SpeechRecognition restarted');
            } catch(e) {
                console.warn('[BOT] Immediate restart failed, retrying in 1s: ' + e.message);
                setTimeout(function() {
                    if (!window.__speechStopped && !window.__restartPending && !window.__botSpeaking) {
                        try { recognition.start(); window.__speechActive = true; } catch(_) {}
                    }
                }, 1000);
            }
        }
    };

    // ── Start: enumerate devices (log only), then start recognition ──
    //
    // By the time this script is injected:
    //   1. Google Meet has already called getUserMedia() for its WebRTC peer connection
    //      (mic unmuted via _ensure_mic_on). Meet's outgoing WebRTC audio track is
    //      PERMANENTLY bound to virtual_mic_source (the TTS output path).
    //   2. pactl set-default-source BotMic has been called in Python, so
    //      any NEW getUserMedia call on this page (including Chrome's internal
    //      SpeechRecognition) will open BotMic.
    //
    // CRITICAL — DO NOT call getUserMedia() here for any reason:
    //   Any getUserMedia() call in the Meet tab (even a briefly-stopped stream) causes
    //   Google Meet to attempt WebRTC renegotiation and switch its outgoing mic track from
    //   virtual_mic_source to the newly-opened device. This permanently breaks TTS audio
    //   routing: pacat writes to virtual_mic → virtual_mic_source → Meet WebRTC, but if
    //   Meet renegotiates to BotMic, TTS audio goes nowhere the candidate can hear.
    //
    // Chrome's SpeechRecognition opens its own getUserMedia in a SEPARATE sandboxed
    // audio service process — completely isolated from the Meet renderer's WebRTC
    // negotiation. It will use the pactl default source = BotMic. No action needed.
    console.log('[BOT] [STT] Enumerating audio devices for diagnostics (no getUserMedia)...');
    navigator.mediaDevices.enumerateDevices()
        .then(function(devices) {
            var audioInputs = devices.filter(function(d) { return d.kind === 'audioinput'; });
            console.log('[BOT] [STT] Audio inputs visible: ' + audioInputs.length);
            audioInputs.forEach(function(d) {
                console.log('STT_DEVICE:' + d.label + '  id=' + d.deviceId);
            });
            var botMic = audioInputs.find(function(d) {
                return d.label && (
                    d.label.toLowerCase().includes('botmic') ||
                    d.label.toLowerCase().includes('botmiccapture') ||
                    d.label.toLowerCase().includes('monitor of virtualsink')
                );
            });
            if (botMic) {
                console.log('🎯 [BOT] [STT] BotMicCapture confirmed visible: ' + botMic.label);
            } else {
                console.log('⚠️  [BOT] [STT] BotMicCapture label not visible — pactl default (BotMic) will be used');
            }
        })
        .catch(function(err) {
            console.warn('[BOT] [STT] enumerateDevices failed: ' + err.message);
        })
        .finally(function() {
            // Start recognition unconditionally.
            // Chrome SpeechRecognition opens its own isolated getUserMedia on pactl default = BotMic.
            // This does NOT touch Meet's existing WebRTC track on virtual_mic_source.
            try {
                recognition.start();
                console.log('[BOT] [STT] SpeechRecognition.start() called — BotMic via pactl default');
            } catch(e) {
                console.error('[BOT] [STT] recognition.start() failed: ' + e.message);
                window.__speechActive = false;
            }
        });

})(%s);
"""


async def inject_speech_recognition(
    page,
    session_id: str,
) -> None:
    """
    Inject the Web Speech API transcription script into *page*.

    Transcripts are emitted as console.log("TRANSCRIPT_EVENT:<text>") and
    captured by the page.on("console") handler in bot.py.
    """
    import json as _json
    js = _SPEECH_API_JS % (_json.dumps(session_id),)

    try:
        await page.evaluate(js)
        msg = "✅ Web Speech API injected — transcripts via console.log TRANSCRIPT_EVENT"
        print(msg)
        await push_log(msg)
    except Exception as e:
        msg = f"❌ Speech injection failed: {e}"
        print(msg)
        await push_log(msg)
        raise
