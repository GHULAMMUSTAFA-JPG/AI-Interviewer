# speech_injector.py
# Web Speech API injection for Google Meet transcription

SPEECH_INJECTION_SCRIPT = """
(function() {
    if (window.__stt_injected) return;
    window.__stt_injected = true;

    // ─── State ───────────────────────────────────────────────────
    let transcript           = "";     // Accumulated confirmed finals (cleared each bot turn)
    let isBotSpeaking        = false;  // Hard gate: true = discard everything
    let botStoppedAt         = 0;      // Timestamp when bot stopped (for echo time gate)
    let silenceTimer         = null;   // Fires VAD_SILENCE_MS after last final → save
    let maxDurTimer          = null;   // Fires MAX_SPEECH_MS after speech starts → force-save
    let recognition          = null;
    let isRunning            = false;
    let interviewActive      = true;
    let restartBackoff       = 1500;   // ms — doubles on 'aborted', resets on success

    // ─── VAD (Voice Activity Detection) state ────────────────────
    // Replaces the old 4500ms fixed silence timer with a 300ms VAD window.
    // When onresult fires, we reset a 300ms timer. If no new onresult fires
    // within 300ms, speech is considered ended and we save immediately.
    // This cuts latency from ~4500ms → ~300ms for response time.
    let vadState             = "IDLE";  // IDLE | SPEAKING | SILENCE_DETECT
    let vadSilenceTimer      = null;    // Fires after VAD_SILENCE_MS of no new finals

    // Expose state for Python health monitoring
    window.__stt_health = function() {
        return { isRunning, restartBackoff, interviewActive, isBotSpeaking, vadState };
    };

    // Python watchdog calls this when it detects isRunning=false for too long
    window.__stt_restart = function() {
        if (!interviewActive) return;
        restartBackoff = 1500;  // reset backoff before forcing restart
        if (recognition && isRunning) {
            // Already running — stop it, onend will schedule a fresh start
            recognition.stop();
        } else if (!isRunning) {
            // Stalled — force a new session immediately
            scheduleRestart(300);
        }
        emit("STT_FORCE_RESTARTED:");
    };

    // ─── Two-phase interrupt state ───────────────────────────────
    // onspeechstart alone is not enough — background noise and room sounds
    // trigger it constantly. We wait for onresult to confirm real speech before
    // sending the interrupt signal to Python.
    let pendingInterruptTimer = null;  // window waiting for onresult confirmation
    let interruptConfirmed    = false; // interrupt already sent this bot turn

    // ─── Constants ───────────────────────────────────────────────
    const VAD_SILENCE_MS       = 300;   // ms of silence after last final → speech ended (was 4500ms)
    // Phase-aware silence: pipeline sets this per phase via window.__set_silence_ms().
    // Default matches VAD_SILENCE_MS. CLOSING=2500ms, TECHNICAL=5000ms, others=4000ms.
    let VAD_SILENCE_MS_dynamic = VAD_SILENCE_MS;
    const ECHO_GATE_MS         = 700;   // ms to ignore STT after bot stops (Google STT queue drains in ~300-500ms)
    const MAX_SPEECH_MS        = 45000; // ms — force-save when onspeechend never fires (technical answers exceed 25s)
    const MIN_CONFIDENCE       = 0.55;  // discard finals below this; 0 = not reported → keep
    const MIN_WORDS            = 2;     // discard saves shorter than this (noise artifacts)
    const INTERRUPT_CONFIRM_MS = 2000;  // ms window to confirm interrupt via onresult
    const MIN_INTERRUPT_WORDS  = 1;     // words required in onresult to confirm real interruption
    const MAX_RESTART_BACKOFF  = 4000;  // ms — max backoff cap for recognition restarts (was 16000)

    // ─── Short-answer lexicon ────────────────────────────────────
    // Single words that are valid interview responses — never discard these
    // even though they fall below MIN_WORDS=2.
    const VALID_SHORT_ANSWERS = new Set([
        "yes","no","yeah","nope","yep","nah","right","correct","wrong",
        "okay","ok","sure","fine","agreed","agree","exactly","absolutely",
        "definitely","certainly","perhaps","maybe","possibly","true","false",
        "never","always","sometimes","often","rarely","unclear","unsure"
    ]);

    // ─── Python bridge ───────────────────────────────────────────
    function emit(tag, data) {
        console.log(tag + (data !== undefined ? JSON.stringify(data) : ""));
    }

    function _clearTimers() {
        if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
        if (maxDurTimer)  { clearTimeout(maxDurTimer);  maxDurTimer  = null; }
        if (vadSilenceTimer) { clearTimeout(vadSilenceTimer); vadSilenceTimer = null; }
        vadState = "IDLE";
    }

    function _clearInterruptState() {
        interruptConfirmed = false;
        if (pendingInterruptTimer) {
            clearTimeout(pendingInterruptTimer);
            pendingInterruptTimer = null;
        }
    }

    function _trySave() {
        // Shared save path used by silence timer and MAX_SPEECH force-save.
        const text  = transcript.trim();
        const words = text ? text.split(/\\s+/).length : 0;
        const isValidShort = words === 1 && VALID_SHORT_ANSWERS.has(text.toLowerCase().replace(/[.!?,]$/, ""));
        if (text && (words >= MIN_WORDS || isValidShort) && !isBotSpeaking) {
            emit("TRANSCRIPT_EVENT:", text);
            transcript = "";
        } else if (text && words < MIN_WORDS && !isValidShort) {
            emit("STT_SHORT_DISCARD:", text);
            transcript = "";
        }
    }

    // ─── Bot gates (Python calls these via page.evaluate) ────────
    window.__tts_started = function() {
        isBotSpeaking = true;
        _clearInterruptState();
        _clearTimers();
        transcript = "";
        emit("TTS_STARTED:");
    };

    window.__tts_ended = function(wasInterrupted) {
        isBotSpeaking = false;
        _clearInterruptState();
        // Echo gate: Google STT queue still has 1-2s of bot audio in flight —
        // always run the full echo gate regardless of interrupt, to prevent
        // the bot's own voice from leaking through as candidate speech.
        botStoppedAt = Date.now();

        // On natural end, clear everything for a fresh start.
        // On interrupt, candidate's speech is accumulating in transcript — keep it.
        if (!wasInterrupted) {
            _clearTimers();
            transcript = "";
        }

        // FIX: Reset backoff so TTS restarts don't compound delays.
        // Without this, repeated stop/start cycles grow restartBackoff to 16s,
        // creating 16-second gaps where NO speech is captured.
        restartBackoff = 1500;

        emit("TTS_ENDED:");
        // Force a fresh recognition session to clear any throttled/aborted state
        // that accumulated while recognition was running during isBotSpeaking=true.
        if (recognition && isRunning) {
            recognition.stop();   // onend fires → scheduleRestart(restartBackoff=1500)
        } else if (!isRunning && interviewActive) {
            scheduleRestart(300);
        }
    };

    window.__stop_interview = function() {
        interviewActive = false;
        _clearTimers();
        _clearInterruptState();
        if (recognition) recognition.stop();
        emit("STT_STOPPED:");
    };

    // ─── Phase-aware silence threshold ───────────────────────────
    // Python calls this after each agent turn when it detects a phase change.
    // Phases that expect short answers (CLOSING) use a shorter silence window
    // to avoid making the candidate wait 4.5s for a response to "yes".
    window.__set_silence_ms = function(ms) {
        VAD_SILENCE_MS_dynamic = Math.max(200, Math.min(8000, ms));
        emit("STT_SILENCE_MS_SET:", VAD_SILENCE_MS_dynamic);
    };

    // ─── Core recognition ─────────────────────────────────────────
    function createRecognition() {
        const r = new webkitSpeechRecognition();
        r.continuous      = true;
        r.interimResults  = true;
        r.lang            = '__STT_LANGUAGE__';
        r.maxAlternatives = 1;

        r.onstart = function() {
            isRunning      = true;
            restartBackoff = 1500;  // reset backoff on every successful start
            emit("STT_ACTIVE:");
        };

        // ── Sub-feature: Interruption detection (two-phase) ──────
        // onspeechstart fires on ANY audio above the engine's threshold — room noise,
        // keyboard clicks, microphone pops. We don't interrupt the bot on onspeechstart
        // alone. Instead we open a 1000ms window and wait for onresult. If onresult
        // delivers ≥1 word, the interrupt is confirmed. If the window expires with no
        // onresult, it was noise — bot continues speaking.
        r.onspeechstart = function() {
            if (Date.now() - botStoppedAt < ECHO_GATE_MS && !isBotSpeaking) return;

            if (isBotSpeaking) {
                // Start confirmation window if not already waiting
                if (!pendingInterruptTimer && !interruptConfirmed) {
                    pendingInterruptTimer = setTimeout(function() {
                        pendingInterruptTimer = null;
                        // Window expired with no onresult — noise, not speech. Ignore.
                    }, INTERRUPT_CONFIRM_MS);
                }
                return;  // Don't emit yet — wait for confirmation
            }

            emit("STT_SPEECH_START:");

            // ── Sub-feature: MAX_SPEECH_MS force-save ─────────────
            if (maxDurTimer) clearTimeout(maxDurTimer);
            maxDurTimer = setTimeout(function() {
                maxDurTimer = null;
                if (isBotSpeaking) return;
                if (silenceTimer) { clearTimeout(silenceTimer); silenceTimer = null; }
                emit("STT_MAX_DURATION_SAVE:");
                _trySave();
            }, MAX_SPEECH_MS);
        };

        // ── Sub-feature: Echo gate + silence timer + noise filters ─
        r.onresult = function(event) {
            // Two-phase interrupt: bot is speaking but we have a pending confirmation.
            // Check if this result contains real speech words.
            if (isBotSpeaking) {
                if (pendingInterruptTimer && !interruptConfirmed) {
                    for (let i = event.resultIndex; i < event.results.length; i++) {
                        const result = event.results[i];
                        const text   = result[0].transcript.trim();
                        const conf   = result[0].confidence;
                        // Require ≥1 word AND confidence above noise threshold (or unreported conf=0)
                        const confOk = (conf === 0 || conf >= 0.4);
                        if (text && text.split(/\\s+/).length >= MIN_INTERRUPT_WORDS && confOk) {
                            // Real speech confirmed — interrupt the bot
                            clearTimeout(pendingInterruptTimer);
                            pendingInterruptTimer = null;
                            interruptConfirmed    = true;
                            transcript            = text + " ";  // preserve candidate's words
                            emit("STT_SPEECH_START:");  // Python: set tts_interrupt=True
                            break;
                        }
                    }
                }
                return;  // Hard gate: discard all further processing while bot speaks
            }

            // Time gate: too soon after bot stopped — Google STT queue still has bot audio
            if (Date.now() - botStoppedAt < ECHO_GATE_MS) return;

            let gotFinal = false;
            for (let i = event.resultIndex; i < event.results.length; i++) {
                const result = event.results[i];
                const text   = result[0].transcript.trim();

                if (result.isFinal) {
                    // ── Sub-feature: MIN_CONFIDENCE filter ─────────
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
                // ── VAD-based speech end detection ──────────────────
                // Instead of waiting 4500ms of silence, we detect speech ended
                // after just 300ms of no new finals. This cuts response latency
                // from ~4500ms → ~300ms.
                vadState = "SPEAKING";
                if (vadSilenceTimer) clearTimeout(vadSilenceTimer);

                vadSilenceTimer = setTimeout(function() {
                    vadSilenceTimer = null;
                    if (vadState === "SPEAKING" && transcript.trim() && !isBotSpeaking) {
                        vadState = "IDLE";
                        emit("STT_VAD_SPEECH_END:");
                        _trySave();
                    } else {
                        vadState = "IDLE";
                    }
                }, VAD_SILENCE_MS_dynamic);
            }
        };

        r.onspeechend = function() {
            if (maxDurTimer) { clearTimeout(maxDurTimer); maxDurTimer = null; }
            if (isBotSpeaking) return;
            emit("STT_SPEECH_END:");
            // VAD handles speech end detection, but onspeechend is a fallback
            // for when Google's speech engine detects end-of-utterance.
            if (!vadSilenceTimer && transcript.trim()) {
                vadState = "SILENCE_DETECT";
                vadSilenceTimer = setTimeout(function() {
                    vadSilenceTimer = null;
                    if (vadState === "SILENCE_DETECT" && !isBotSpeaking) {
                        vadState = "IDLE";
                        emit("STT_VAD_SPEECH_END:");
                        _trySave();
                    } else {
                        vadState = "IDLE";
                    }
                }, VAD_SILENCE_MS_dynamic);
            }
        };

        // ── Sub-feature: Abort recovery with exponential backoff ───
        r.onerror = function(event) {
            isRunning = false;
            if (event.error === 'no-speech') {
                scheduleRestart(1500);
            } else if (event.error === 'aborted') {
                emit("STT_ERROR:", "aborted (retry in " + restartBackoff + "ms)");
                const delay   = restartBackoff;
                restartBackoff = Math.min(restartBackoff * 2, MAX_RESTART_BACKOFF);
                scheduleRestart(delay);
            } else if (event.error === 'not-allowed') {
                emit("STT_ERROR:", "not-allowed — microphone permission denied");
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
                    restartBackoff = Math.min(restartBackoff * 2, MAX_RESTART_BACKOFF);
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


async def inject_speech_recognition(page, session_id: str, language: str = "en-US") -> None:
    script = SPEECH_INJECTION_SCRIPT.replace("'__STT_LANGUAGE__'", f"'{language}'")
    await page.evaluate(script)
