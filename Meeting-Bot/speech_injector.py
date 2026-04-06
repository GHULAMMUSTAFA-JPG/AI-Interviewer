# speech_injector.py
# Web Speech API injection for Google Meet transcription
# FIXED: Eliminates duplication, no-speech gaps, and TTS echo

SPEECH_INJECTION_SCRIPT = """
(function() {
    // ─── Guard: don't inject twice ───────────────────────────────
    if (window.__stt_injected) return;
    window.__stt_injected = true;

    // ─── State ───────────────────────────────────────────────────
    let finalTranscript = "";      // Accumulated CONFIRMED finals (cleared when bot speaks)
    let sessionTranscript = "";    // Current recognition session's best result
    let isBotSpeaking = false;     // Gate: true = bot TTS is playing
    let speechActive = false;      // True while candidate is speaking
    let silenceTimer = null;
    let recognition = null;
    let isRunning = false;
    let interviewActive = true;
    let botStoppedAt = 0;

    // ─── Configuration ───────────────────────────────────────────
    const SILENCE_MS = 1500;       // Save after 1.5s silence (was 2s, faster now)
    const ECHO_GATE_MS = 2000;     // After bot stops, ignore STT for 2s (covers echo tail + natural pause)

    // ─── Python bridge ───────────────────────────────────────────
    // Python reads these via page.on('console')
    function emit(tag, data) {
        console.log(tag + (data !== undefined ? JSON.stringify(data) : ""));
    }

    // ─── TTS gate (Python calls these via page.evaluate) ─────────
    window.__tts_started = function() {
        isBotSpeaking = true;
        // Clear any pending save — bot is about to speak
        if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
        // Reset session so bot's voice doesn't contaminate buffer
        sessionTranscript = "";
        // KEY FIX: Clear finalTranscript when bot starts (new turn begins)
        // This prevents accumulation across turns
        finalTranscript = "";
        emit("TTS_STARTED:");
    };

    window.__tts_ended = function() {
        isBotSpeaking = false;
        botStoppedAt = Date.now();
        emit("TTS_ENDED:");
    };

    window.__stop_interview = function() {
        interviewActive = false;
        if (recognition) recognition.stop();
        emit("STT_STOPPED:");
    };

    // ─── Core recognition logic ───────────────────────────────────
    function createRecognition() {
        const r = new webkitSpeechRecognition();
        r.continuous = true;
        r.interimResults = true;
        r.lang = 'en-US';  // Try 'hi-IN' for Urdu/English code-switching candidates
        r.maxAlternatives = 1;

        r.onstart = function() {
            isRunning = true;
            emit("STT_ACTIVE:");
        };

        r.onspeechstart = function() {
            // Ignore if bot is speaking or echo gate is active
            if (isBotSpeaking) return;
            if (Date.now() - botStoppedAt < ECHO_GATE_MS) return;

            if (!speechActive) {
                speechActive = true;
                emit("STT_SPEECH_START:");
            }

            // Cancel any pending silence save
            if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
        };

        r.onresult = function(event) {
            // Hard gate: if bot is speaking, discard everything
            if (isBotSpeaking) return;
            // Echo gate: discard results too soon after bot stops
            if (Date.now() - botStoppedAt < ECHO_GATE_MS) return;

            // ── KEY FIX: Build transcript only from NEW results ──
            // event.resultIndex tells us where new results START
            // We collect the best transcript for this session only

            let interimText = "";
            let newFinalText = "";

            for (let i = event.resultIndex; i < event.results.length; i++) {
                const result = event.results[i];
                const text = result[0].transcript.trim();

                if (result.isFinal) {
                    newFinalText += text + " ";
                } else {
                    interimText = text; // latest interim (not cumulative)
                }
            }

            // Add new finals to session transcript
            if (newFinalText) {
                sessionTranscript += newFinalText;
                emit("STT_INTERIM:", sessionTranscript.trim());
            } else if (interimText) {
                emit("STT_INTERIM:", (sessionTranscript + interimText).trim());
            }
        };

        r.onspeechend = function() {
            if (isBotSpeaking) return;

            emit("STT_SPEECH_END:");

            // Merge session into final transcript
            if (sessionTranscript.trim()) {
                finalTranscript += sessionTranscript;
                sessionTranscript = "";
            }

            // Wait for silence before saving
            if (silenceTimer) clearTimeout(silenceTimer);
            silenceTimer = setTimeout(function() {
                const text = finalTranscript.trim();
                if (text && !isBotSpeaking) {
                    emit("TRANSCRIPT_EVENT:", text);
                    finalTranscript = "";  // Clear after saving
                    speechActive = false;
                }
                silenceTimer = null;
            }, SILENCE_MS);
        };

        r.onerror = function(event) {
            isRunning = false;
            if (event.error === 'no-speech') {
                // Normal — candidate wasn't speaking. Just restart.
                // Don't emit an error, don't lose the buffer.
                scheduleRestart(100);  // KEY FIX: 100ms restart (was 2s)
            } else if (event.error === 'audio-capture') {
                emit("STT_ERROR:", "audio-capture — mic not available");
                scheduleRestart(2000);
            } else if (event.error === 'not-allowed') {
                emit("STT_ERROR:", "not-allowed — permission denied");
                // Don't restart — this needs manual fix
            } else if (event.error === 'network') {
                emit("STT_ERROR:", "network — Google STT unreachable");
                scheduleRestart(3000);
            } else {
                emit("STT_ERROR:", event.error);
                scheduleRestart(1000);
            }
        };

        r.onend = function() {
            isRunning = false;
            // ── KEY FIX: On end, merge any remaining session text ──
            // recognition.onend fires AFTER onspeechend sometimes loses finals
            // So we merge here too as a safety net
            if (sessionTranscript.trim()) {
                finalTranscript += sessionTranscript;
                sessionTranscript = "";
            }
            if (interviewActive && !isBotSpeaking) {
                scheduleRestart(150);
            }
        };

        return r;
    }

    function scheduleRestart(delayMs) {
        if (!interviewActive) return;
        setTimeout(function() {
            if (!isRunning && interviewActive) {
                recognition = createRecognition();
                try {
                    recognition.start();
                } catch(e) {
                    emit("STT_ERROR:", "start failed: " + e.message);
                    scheduleRestart(500);
                }
            }
        }, delayMs);
    }

    // ─── Start ───────────────────────────────────────────────────
    recognition = createRecognition();
    recognition.start();
    emit("STT_INJECTED:");
})();
"""


async def inject_speech_recognition(page, session_id: str) -> None:
    """
    Inject Web Speech API recognition script into the page.
    
    Args:
        page: Playwright page object
        session_id: Interview/session ID for logging
    """
    await page.evaluate(SPEECH_INJECTION_SCRIPT)
