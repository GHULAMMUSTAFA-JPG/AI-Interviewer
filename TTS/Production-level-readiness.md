● Production Readiness Report — TTS Service                                                                                                                
  ---                                                                                                                                                      Overall Score: 5.8/10 — Development Complete, Not Production Ready                                                                                     
                                                                                                                                                           ┌───────────────────────┬───────┬──────────────────────────────────────────────────────┐                                                                 │       Category        │ Score │                        Notes                         │                                                                 ├───────────────────────┼───────┼──────────────────────────────────────────────────────┤                                                                 │ Architecture & Design │ 8/10  │ Clean, well-separated, good rationale                │                                                               
  ├───────────────────────┼───────┼──────────────────────────────────────────────────────┤
  │ Code Quality          │ 7/10  │ Solid, but missing retry/recovery patterns           │
  ├───────────────────────┼───────┼──────────────────────────────────────────────────────┤
  │ Test Coverage         │ 6/10  │ Unit/integration present, no e2e/chaos tests         │
  ├───────────────────────┼───────┼──────────────────────────────────────────────────────┤
  │ Error Handling        │ 5/10  │ Good for API errors, poor for retries                │
  ├───────────────────────┼───────┼──────────────────────────────────────────────────────┤
  │ Documentation         │ 8/10  │ Excellent, missing ops/runbooks                      │
  ├───────────────────────┼───────┼──────────────────────────────────────────────────────┤
  │ Observability         │ 3/10  │ Logs only — no metrics, no tracing, no health check  │
  ├───────────────────────┼───────┼──────────────────────────────────────────────────────┤
  │ Security              │ 4/10  │ Critical: real API key committed to repo             │
  ├───────────────────────┼───────┼──────────────────────────────────────────────────────┤
  │ Deployment            │ 6/10  │ Docker works, missing health checks, resource limits │
  ├───────────────────────┼───────┼──────────────────────────────────────────────────────┤
  │ Resilience            │ 4/10  │ No retries, no circuit breaker, no fallback          │
  ├───────────────────────┼───────┼──────────────────────────────────────────────────────┤
  │ Performance           │ 7/10  │ Meets latency SLOs, unbounded audio buffer risk      │
  └───────────────────────┴───────┴──────────────────────────────────────────────────────┘

  ---
  What You Have

  Solid foundations

  - MongoDB Change Streams — push-based, no polling, ~10ms delivery
  - ElevenLabs streaming — first audio in ~300ms, no buffering delay
  - NATS interrupt — asyncio.Event stops audio within one chunk cycle (<100ms)
  - Graceful shutdown — SIGTERM/SIGINT handled, components closed in order
  - Reconnection logic — MongoDB watch retries with exponential backoff (5 attempts)
  - NATS auto-reconnect — nats-py handles reconnection transparently
  - Audio device fallback — if configured device fails, falls back to system default
  - Stateless synthesizer — failed calls don't affect next synthesis
  - Typed exception hierarchy — ElevenLabs errors are structured and catchable
  - JSON structured logging — parseable by Datadog, ELK, CloudWatch
  - GridFS audio storage — every utterance preserved, replayable
  - Pydantic schema validation — MongoDB documents validated on arrival
  - Full test suite — 42 passed, 1 skipped, all external calls mocked

  ---
  What Is Missing

  CRITICAL — Must fix before any production use

  1. Real API key committed to repo
  - ELEVENLABS_API_KEY is a live key visible in .env in the repo
  - Action: Rotate the key immediately, add .env to .gitignore, inject via environment only

  2. No retry logic
  - ElevenLabs 429 (rate limit) and 5xx errors mark the document as "played" and move on — no retry, no backoff
  - A single transient failure permanently loses that utterance
  - Need: retry with exponential backoff + jitter for 429 and 5xx (max 3 attempts)

  3. No health check
  - Docker and any orchestrator (Kubernetes, ECS) have no way to know if the service is alive
  - Container could be hung/deadlocked and appear healthy
  - Need: HEALTHCHECK in Dockerfile, /health endpoint checking MongoDB + NATS connectivity

  4. No input validation
  - doc.text is sent directly to ElevenLabs with no length or content check
  - audio_buffer is an unbounded list — a 10-minute utterance would accumulate ~130MB in RAM
  - Need: max text length guard (~5000 chars), max audio buffer cap

  5. Docker runs as root
  - Dockerfile has no USER directive — container runs as root
  - Need: create a non-root user in the image

  ---
  HIGH — Required for stable production

  6. No observability (metrics, tracing)
  - Zero Prometheus metrics — can't alert on synthesis failure rate, latency, queue depth
  - No trace IDs — can't correlate a single synthesis request across logs
  - Need: counters (documents_processed, synthesis_errors, interrupts), histograms (synthesis_duration_ms), Prometheus endpoint

  7. No circuit breaker
  - If ElevenLabs is down for an hour, the service fails every request for that hour with no fallback
  - Need: open the circuit after N consecutive failures, optionally fall back to cached audio

  8. No dead-letter handling
  - Failed syntheses are silently discarded (marked "played")
  - No way to know which utterances were lost or replay them
  - Need: write failures to a tts_failed collection with error reason and retry count

  9. Unbounded stream timeout
  - httpx has a 10s connect timeout but no timeout on the full streaming response
  - If ElevenLabs sends chunks slowly, synthesis hangs indefinitely blocking the main loop
  - Need: read timeout on the stream

  10. No MongoDB/NATS authentication
  - Both services run with no credentials — rely entirely on network isolation
  - In production any process on the Docker network can read all interview transcripts
  - Need: MongoDB RBAC user for TTS service, NATS credentials

  11. No resource limits in Docker
  - TTS container can consume unlimited CPU and memory
  - One stuck synthesis could starve other containers
  - Need: mem_limit, cpus in docker-compose

  ---
  MEDIUM — Should have for maintainability

  12. No logging context propagation
  - Logs emit logger, level, message but not interview_id, speaker, trace_id
  - Hard to trace a single synthesis request across 10 log lines
  - Need: include interview_id and document_id in every log from _process_transcript onward

  13. No data retention policy
  - GridFS grows unbounded — every utterance ever synthesised is stored forever
  - Need: TTL index or cleanup job on the audio.files collection

  14. audio_url="played" magic string
  - Used as a sentinel in 3+ places — should be a named constant or enum
  - Currently if this string changes, the whole pipeline silently breaks

  15. No graceful drain on shutdown
  - SIGTERM closes components immediately
  - If synthesis is in progress, audio is cut off and document may not be updated
  - Need: wait for in-flight _process_transcript to complete (with 30s timeout) before closing

  16. Docker Compose log limits missing
  - No logging driver limits on containers — logs can fill disk
  - Need: json-file driver with max-size: "10m", max-file: "3"

  ---
  Error Recovery Gap Table

  ┌─────────────────────────────┬─────────────────────────────────────┬─────────────────────────────────────────┐
  │      Failure Scenario       │          Current Behaviour          │       Production Behaviour Needed       │
  ├─────────────────────────────┼─────────────────────────────────────┼─────────────────────────────────────────┤
  │ ElevenLabs 429              │ Mark as played, move on             │ Retry after Retry-After header (or 60s) │
  ├─────────────────────────────┼─────────────────────────────────────┼─────────────────────────────────────────┤
  │ ElevenLabs 5xx              │ Mark as played, move on             │ Retry 3x with backoff, then dead-letter │
  ├─────────────────────────────┼─────────────────────────────────────┼─────────────────────────────────────────┤
  │ ElevenLabs hangs mid-stream │ Hangs forever                       │ Stream read timeout (e.g. 60s)          │
  ├─────────────────────────────┼─────────────────────────────────────┼─────────────────────────────────────────┤
  │ MongoDB Change Stream drops │ Reconnects (5 attempts then stops)  │ Alert + auto-restart via supervisor     │
  ├─────────────────────────────┼─────────────────────────────────────┼─────────────────────────────────────────┤
  │ MongoDB connection lost     │ Crash                               │ Connection pool reconnect + alert       │
  ├─────────────────────────────┼─────────────────────────────────────┼─────────────────────────────────────────┤
  │ Audio device offline        │ Falls back to system default        │ Log + metric, continue                  │
  ├─────────────────────────────┼─────────────────────────────────────┼─────────────────────────────────────────┤
  │ GridFS write fails          │ Crash, document not updated         │ Retry + alert                           │
  ├─────────────────────────────┼─────────────────────────────────────┼─────────────────────────────────────────┤
  │ NATS down                   │ Degrades gracefully (no interrupts) │ Log warning, continue without interrupt │
  └─────────────────────────────┴─────────────────────────────────────┴─────────────────────────────────────────┘

  ---
  Phase Plan

  Phase 1 — Blockers (1–2 weeks)

  1. Rotate and remove committed API key, add .env to .gitignore
  2. Add retry logic for 429 and 5xx (exponential backoff + jitter)
  3. Add stream read timeout to ElevenLabs httpx client
  4. Add text length validation and audio buffer cap
  5. Add HEALTHCHECK to Dockerfile + non-root user
  6. Add resource limits and log rotation to docker-compose
  7. Add MongoDB auth credentials to docker-compose

  Phase 2 — Stability (2–4 weeks)

  1. Prometheus metrics endpoint (counters + histograms)
  2. Dead-letter collection for failed syntheses
  3. Structured logging with interview_id + document_id context
  4. Circuit breaker for ElevenLabs
  5. Graceful shutdown drain (wait for in-flight synthesis)
  6. Data retention / GridFS TTL cleanup

  Phase 3 — Observability & Scale (ongoing)

  1. OpenTelemetry distributed tracing
  2. Grafana dashboard + alert rules
  3. Session-targeted interrupt support (parse NATS payload)
  4. NATS authentication
  5. Cost tracking (ElevenLabs API calls per session)
  6. Local TTS fallback (Piper) when ElevenLabs is unavailable

  ---
  Bottom Line

  The core pipeline works correctly and is well-engineered. The architecture decisions (Change Streams, streaming PCM, NATS interrupt) are sound. What's 
  missing is the operational layer: retries, observability, security hardening, and health checks. Phase 1 alone would take it from "works in dev" to    
  "safe to run in production".
