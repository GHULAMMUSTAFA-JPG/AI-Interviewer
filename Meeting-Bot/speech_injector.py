# speech_injector.py
# Web Speech API injection for Google Meet transcription

SPEECH_INJECTION_SCRIPT = """
(function() {
    if (window.__stt_injected) return;
    window.__stt_injected = true;

    // ─── State ───────────────────────────────────────────────────
    let transcript      = "";     // Accumulated confirmed finals (cleared each bot turn)
    let isBotSpeaking   = false;  // Hard gate: true = discard everything
    let botStoppedAt    = 0;      // Timestamp when bot stopped (for echo time gate)
    let silenceTimer    = null;   // Fires SILENCE_MS after last final → save
    let maxDurTimer     = null;   // Fires MAX_SPEECH_MS after speech starts → force-save
    let recognition     = null;
    let isRunning       = false;
    let interviewActive = true;
    let restartBackoff  = 1500;   // ms — doubles on 'aborted', resets on success

    // ─── Constants ───────────────────────────────────────────────
    const SILENCE_MS     = 1000;  // ms of silence after last confirmed word → save
    const ECHO_GATE_MS   = 1500;  // ms to ignore STT after bot stops (covers Google STT queue)
    const MAX_SPEECH_MS  = 25000; // ms — force-save when onspeechend never fires (bg noise)
    const MIN_CONFIDENCE = 0.55;  // discard finals below this; 0 = not reported → keep
    const MIN_WORDS      = 2;     // discard saves shorter than this (noise artifacts)

    // ─── Python bridge ───────────────────────────────────────────
    function emit(tag, data) {
        console.log(tag + (data !== undefined ? JSON.stringify(data) : ""));
    }

    function _clearTimers() {
        if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
        if (maxDurTimer)  { clearTimeout(maxDurTimer);  maxDurTimer  = null; }
    }

    function _trySave() {
        // Shared save path used by silence timer and MAX_SPEECH force-save.
        const text  = transcript.trim();
        const words = text ? text.split(/\s+/).length : 0;
        if (text && words >= MIN_WORDS && !isBotSpeaking) {
            emit("TRANSCRIPT_EVENT:", text);
            transcript = "";
        } else if (text && words < MIN_WORDS) {
            emit("STT_SHORT_DISCARD:", text);
            transcript = "";
        }
    }

    // ─── Bot gates (Python calls these via page.evaluate) ────────
    window.__tts_started = function() {
        isBotSpeaking = true;
        _clearTimers();
        transcript = "";
        emit("TTS_STARTED:");
    };

    window.__tts_ended = function(wasInterrupted) {
        isBotSpeaking = false;
        // Interrupt: candidate's speech stopped the bot — skip echo gate so those
        //            interrupting words are captured immediately.
        // Natural end: Google STT queue still has 1-2s of bot audio in flight —
        //              run the full echo gate so it doesn't contaminate the transcript.
        botStoppedAt = wasInterrupted ? Date.now() - ECHO_GATE_MS : Date.now();

        // CRITICAL: On interrupt, the candidate is still speaking — their words
        // are accumulating in `transcript`. Don't clear it or we lose their speech.
        // On natural end, clear everything for a fresh start.
        if (!wasInterrupted) {
            _clearTimers();
            transcript = "";
        }
        emit("TTS_ENDED:");
        // Force a fresh recognition session to clear any throttled/aborted state
        // that accumulated while recognition was running during isBotSpeaking=true.
        if (recognition && isRunning) {
            recognition.stop();   // onend fires → scheduleRestart(restartBackoff)
        } else if (!isRunning && interviewActive) {
            scheduleRestart(300);
        }
    };

    window.__stop_interview = function() {
        interviewActive = false;
        _clearTimers();
        if (recognition) recognition.stop();
        emit("STT_STOPPED:");
    };

    // ─── Core recognition ─────────────────────────────────────────
    function createRecognition() {
        const r = new webkitSpeechRecognition();
        r.continuous      = true;
        r.interimResults  = true;
        r.lang            = 'en-US';
        r.maxAlternatives = 1;

        r.onstart = function() {
            isRunning      = true;
            restartBackoff = 1500;  // reset backoff on every successful start
            emit("STT_ACTIVE:");
        };

        // ── Sub-feature: Interruption detection ──────────────────
        // Fires when the recognition engine detects the start of speech.
        // If bot is currently speaking, this is a candidate interruption — emit
        // STT_SPEECH_START: so Python can set tts_interrupt=True and kill TTS.
        // If bot already finished, check echo gate before marking speech active.
        r.onspeechstart = function() {
            if (Date.now() - botStoppedAt < ECHO_GATE_MS && !isBotSpeaking) return;
            emit("STT_SPEECH_START:");

            // ── Sub-feature: MAX_SPEECH_MS force-save ─────────────
            // Start (or restart) the max-duration guard whenever speech begins.
            // Prevents the recognition session from staying open for minutes due
            // to background noise, which would delay the next LLM turn.
            if (maxDurTimer) clearTimeout(maxDurTimer);
            if (!isBotSpeaking) {
                maxDurTimer = setTimeout(function() {
                    maxDurTimer = null;
                    if (isBotSpeaking) return;
                    if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
                    emit("STT_MAX_DURATION_SAVE:");
                    _trySave();
                }, MAX_SPEECH_MS);
            }
        };

        // ── Sub-feature: Echo gate + silence timer + noise filters ─
        r.onresult = function(event) {
            // Hard gate: bot is speaking — discard everything
            if (isBotSpeaking) return;
            // Time gate: too soon after bot stopped — Google STT queue still has bot audio
            if (Date.now() - botStoppedAt < ECHO_GATE_MS) return;

            let gotFinal = false;
            for (let i = event.resultIndex; i < event.results.length; i++) {
                const result = event.results[i];
                const text   = result[0].transcript.trim();

                if (result.isFinal) {
                    // ── Sub-feature: MIN_CONFIDENCE filter ─────────
                    // Discard finals the recognition engine is unsure about.
                    // confidence=0 means "not reported by this browser" → keep.
                    const conf = result[0].confidence;
                    if (conf > 0 && conf < MIN_CONFIDENCE) {
                        emit("STT_LOW_CONF:", {text: text, confidence: conf});
                        continue;
                    }
                    transcript += text + " ";
                    gotFinal    = true;
                } else {
                    emit("STT_INTERIM:", (transcript + text).trim());
                }
            }

            if (gotFinal) {
                // ── Sub-feature: Silence timer ──────────────────────
                // Reset the window after each new confirmed word.
                // Fires SILENCE_MS after the LAST word — not the first.
                if (silenceTimer) clearTimeout(silenceTimer);
                silenceTimer = setTimeout(function() {
                    silenceTimer = null;
                    _trySave();
                }, SILENCE_MS);
            }
        };

        r.onspeechend = function() {
            // Speech paused — the silence timer set in onresult handles saving.
            // Cancel the max-duration guard since speech ended naturally.
            if (maxDurTimer) { clearTimeout(maxDurTimer); maxDurTimer = null; }
            if (isBotSpeaking) return;
            emit("STT_SPEECH_END:");
            // If no silence timer is running (onspeechend fired with no onresult finals
            // — pure noise detection), check if we have accumulated transcript to save.
            if (!silenceTimer && transcript.trim()) {
                silenceTimer = setTimeout(function() {
                    silenceTimer = null;
                    _trySave();
                }, SILENCE_MS);
            }
        };

        // ── Sub-feature: Abort recovery with exponential backoff ───
        r.onerror = function(event) {
            isRunning = false;
            if (event.error === 'no-speech') {
                // Normal — nothing detected in the audio stream.
                // 1500ms prevents Chrome from throttling after rapid restart cycles.
                scheduleRestart(1500);
            } else if (event.error === 'aborted') {
                // Chrome throttled the speech service — back off exponentially.
                emit("STT_ERROR:", "aborted (retry in " + restartBackoff + "ms)");
                const delay   = restartBackoff;
                restartBackoff = Math.min(restartBackoff * 2, 16000);
                scheduleRestart(delay);
            } else if (event.error === 'not-allowed') {
                emit("STT_ERROR:", "not-allowed — microphone permission denied");
                // Don't restart — requires manual fix
            } else if (event.error === 'network') {
                emit("STT_ERROR:", "network — Google STT unreachable");
                scheduleRestart(3000);
            } else {
                emit("STT_ERROR:", event.error);
                scheduleRestart(1500);
            }
        };

        r.onend = function() {
            isRunning = false;
            if (interviewActive && !isBotSpeaking) {
                // Use same backoff as no-speech to prevent Chrome throttling.
                // 300ms was too aggressive — Chrome rejects rapid restarts with 'aborted'.
                scheduleRestart(restartBackoff);
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
                    emit("STT_ERROR:", "start() threw: " + e.message);
                    restartBackoff = Math.min(restartBackoff * 2, 16000);
                    scheduleRestart(restartBackoff);
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
    await page.evaluate(SPEECH_INJECTION_SCRIPT)
