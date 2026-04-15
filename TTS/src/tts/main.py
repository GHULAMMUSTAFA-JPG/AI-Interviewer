"""TTS Service entry point."""
import asyncio
import json
import logging
import os
import signal
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClient

from .config import config
from .logging_config import setup_logging
from .tts_queue import TranscriptListener
from .edge_synthesizer import EdgeTTSSynthesizer
from .audio_player import AudioPlayer
from .interrupt_handler import InterruptHandler
from .status_updater import TranscriptUpdater
from models.tts_queue import TranscriptDocument

logger = logging.getLogger(__name__)


class TTSService:
    """Main TTS service orchestrator — Edge TTS branch (free, no API key)."""

    def __init__(self) -> None:
        self._mongo_client = AsyncIOMotorClient(config.mongodb_uri)
        self._transcript_listener = TranscriptListener(self._mongo_client)

        # Edge TTS only on this branch
        self._synthesizer = EdgeTTSSynthesizer()
        logger.info(f"Using Edge TTS synthesizer (voice={config.edge_tts_voice})")

        self._player = AudioPlayer()
        self._interrupt_handler = InterruptHandler(self._mongo_client)
        self._transcript_updater = TranscriptUpdater(self._mongo_client)
        self._running = False
        # Tracks interviews that already have a pending resume to prevent cascade
        self._pending_resumes: set[str] = set()

    async def start(self) -> None:
        """Start TTS service."""
        logger.info("Starting TTS Service (MongoDB interrupt mode)...")
        logger.info(f"Edge TTS active (free, no API key) — voice={config.edge_tts_voice}")

        self._running = True

        # Replay any agent transcripts missed while TTS was down
        async for doc in self._transcript_listener.backfill():
            if not self._running:
                break
            await self._process_transcript(doc)

        async for doc in self._transcript_listener.watch():
            if not self._running:
                break
            await self._process_transcript(doc)
            # Backpressure: if more agent transcripts are queued for same interview,
            # pause 1s between them. Back-to-back messages with no gap sounds unnatural.
            try:
                pending = await self._transcript_listener.count_pending(doc.interview_id)
                if pending > 0:
                    await asyncio.sleep(1.0)
            except Exception:
                pass  # Never block on backpressure check

    @staticmethod
    async def _buffered(source, buffer: list):
        """Tee an async generator: yield each chunk and accumulate in buffer."""
        async for chunk in source:
            buffer.append(chunk)
            yield chunk

    @staticmethod
    async def _chunks_from_bytes(data: bytes):
        """Yield PCM bytes in chunk_size pieces (for DB cache playback)."""
        for i in range(0, len(data), config.chunk_size):
            yield data[i : i + config.chunk_size]

    async def _set_tts_status(self, interview_id: str, status: str, interrupted: bool = False) -> None:
        """Write TTS status to Redis AND publish to echo guard pub/sub channel."""
        import time as _time
        try:
            from .redis_client import get_redis
            redis = await get_redis()
            await redis.hset(
                f"tts:{interview_id}:status",
                mapping={"status": status, "updated_at": str(_time.time())}
            )
            # Publish to echo guard pub/sub channel (Meeting-Bot subscribes to this)
            await redis.publish(
                f"tts:{interview_id}:status_events",
                json.dumps({
                    "status": status,
                    "interrupted": interrupted,
                    "timestamp": _time.time()
                })
            )
        except Exception:
            pass

    async def _wait_for_guard_ready(self, interview_id: str, timeout: float = 0.5) -> bool:
        """Wait for echo guard to publish guard_ready (confirms __tts_started() called).

        Returns True if guard_ready received, False on timeout.
        Falls back gracefully if Redis is unavailable.
        """
        try:
            from .redis_client import get_redis
            redis = await get_redis()
            # Publish arm_ready so echo guard knows to send guard_ready
            await redis.publish(f"tts:{interview_id}:arm_ready", "1")
            # Wait for guard_ready with timeout
            pubsub = redis.pubsub()
            await pubsub.subscribe(f"tts:{interview_id}:guard_ready")
            try:
                async def _listen():
                    async for msg in pubsub.listen():
                        if msg["type"] == "message":
                            return True
                    return False
                return await asyncio.wait_for(_listen(), timeout=timeout)
            finally:
                await pubsub.unsubscribe(f"tts:{interview_id}:guard_ready")
        except Exception:
            return False  # Redis unavailable — proceed without handshake

    async def _process_transcript(self, doc: TranscriptDocument) -> None:
        """Synthesize (or replay from DB cache), play, and save audio.

        Before playing: arm the interrupt handler so Meeting-Bot can stop us
        mid-stream by setting tts_interrupt=True on the interview document.
        After playing (naturally or interrupted): disarm so bot_speaking=False
        and Meeting-Bot stops sending interrupt signals.
        """
        logger.info(
            f"Processing transcript: interview={doc.interview_id} chars={len(doc.text)}"
        )
        logger.info(f"[TTS TEXT] \"{doc.text[:300]}\"")

        await self._set_tts_status(doc.interview_id, "speaking")
        # Arm: mark bot as speaking, clear stale interrupt, start watching.
        await self._interrupt_handler.arm(doc.interview_id)

        # 2.2: Publish estimated audio duration so echo guard can set dynamic echo gate.
        # Edge TTS s16le 22050Hz mono → ~44100 bytes/sec. Estimate from text length.
        # Rough heuristic: ~5 chars/word, ~150 words/min → ~12.5ms per char.
        # Capped at 1500ms per the spec — matches JS __set_echo_gate cap.
        estimated_audio_ms = min(int(len(doc.text) * 12.5), 1500)
        try:
            from .redis_client import get_redis as _get_redis
            _redis = await _get_redis()
            await _redis.setex(
                f"tts:{doc.interview_id}:audio_duration_ms",
                30,  # 30s TTL — only relevant during this utterance
                str(estimated_audio_ms)
            )
        except Exception:
            pass

        # 4.1: arm/guard handshake — wait for echo guard to confirm __tts_started()
        # has been called in the browser before we play audio. Replaces guessed 0.3s sleep.
        # Echo guard publishes tts:{id}:guard_ready after __tts_started() succeeds.
        # Timeout 500ms: if guard_ready never arrives, proceed anyway (no regression).
        guard_ready = await self._wait_for_guard_ready(doc.interview_id, timeout=0.5)
        if not guard_ready:
            logger.debug(
                f"guard_ready timeout (proceeding): interview={doc.interview_id} — "
                f"echo guard may not be active"
            )

        # STALE CHECK (Position 2 gap):
        # If the candidate spoke again between the pipeline saving this response and
        # now, their T2 transcript is already in DB. Playing a stale response would
        # result in two bot replies back-to-back. Skip instead — the pipeline for T2
        # will generate a coherent reply using the full conversation context.
        #
        # We mark (not delete) R1 so turn count stays accurate. TTS won't retry
        # because audio_url is no longer null.
        try:
            db = self._mongo_client[config.mongodb_db]
            from bson import ObjectId
            newer_candidate = await db.transcripts.find_one({
                "interview_id": doc.interview_id,
                "speaker": "candidate",
                "_id": {"$gt": ObjectId(str(doc.id))},
            })
            if newer_candidate:
                logger.info(
                    f"[STALE TTS] Skipping response {doc.id} — "
                    f"newer candidate message {newer_candidate['_id']} exists"
                )
                await db.transcripts.update_one(
                    {"_id": ObjectId(str(doc.id))},
                    {"$set": {"audio_url": "tts_skipped"}},
                )
                await self._interrupt_handler.disarm(doc.interview_id)
                await self._set_tts_status(doc.interview_id, "idle")
                return
        except Exception as stale_err:
            # Never block playback on a failed stale check
            logger.warning(f"[STALE TTS] Check failed (proceeding): {stale_err}")

        try:
            if doc.audio_data:
                # --- Cache hit: integrity check then play ---
                # A partial audio save (interrupted mid-synthesis) would produce
                # corrupted PCM. Verify length matches stored metadata before playing.
                expected_len = getattr(doc, "audio_length_bytes", None)
                actual_len = len(doc.audio_data)
                cache_valid = expected_len is None or actual_len == expected_len
                if not cache_valid:
                    logger.warning(
                        f"Cache integrity mismatch for {doc.id}: "
                        f"expected {expected_len}B, got {actual_len}B — re-synthesizing"
                    )

                if doc.audio_data and cache_valid:
                    logger.info(
                        f"Playing from DB cache: {doc.id} "
                        f"({actual_len / 1024:.1f} KB)"
                    )
                    await self._player.play(
                        self._chunks_from_bytes(doc.audio_data),
                        self._interrupt_handler.stop_event,
                    )
                    await self._transcript_updater.mark_played(doc.id, doc.audio_data)

            if not doc.audio_data or not cache_valid:
                # --- Cache miss (or corrupted cache): synthesize from ElevenLabs ---
                audio_buffer: list[bytes] = []

                # Wrap synthesis + playback in a 60-second timeout
                try:
                    raw_chunks = self._synthesizer.synthesize(doc.text)
                    teed = self._buffered(raw_chunks, audio_buffer)

                    await asyncio.wait_for(
                        self._player.play(teed, self._interrupt_handler.stop_event),
                        timeout=60.0
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        f"TTS synthesis/playback timed out after 60s for: {doc.id} "
                        f"text='{doc.text[:80]}...'"
                    )
                    audio_data = b"".join(audio_buffer) if audio_buffer else None
                    await self._transcript_updater.mark_played(doc.id, audio_data)
                    return
                except Exception as audio_exc:
                    # Audio device unavailable — drain generator to fill buffer
                    logger.warning(
                        f"Audio playback unavailable: {audio_exc} — synthesizing to DB only"
                    )
                    try:
                        async for _ in teed:
                            pass
                    except Exception:
                        pass

                audio_data = b"".join(audio_buffer) if audio_buffer else None
                await self._transcript_updater.mark_played(doc.id, audio_data)
                if audio_data:
                    logger.info(
                        f"Transcript completed: {doc.id} "
                        f"audio saved {len(audio_data) / 1024:.1f} KB"
                    )

            if self._interrupt_handler.stop_event.is_set():
                logger.info(f"Transcript interrupted by candidate: {doc.id}")
                # Only schedule resume if not already pending AND this isn't itself
                # a resumed transcript (prevents cascade of re-resumes).
                # If a resumed response is also interrupted, we do NOT reschedule —
                # the candidate is actively speaking; the pipeline will respond to them.
                # Silence timeout (180s) handles the case where they say nothing further.
                is_resume = getattr(doc, "metadata", None) and (
                    doc.metadata or {}
                ).get("resumed_after_interrupt")
                if not is_resume and doc.interview_id not in self._pending_resumes:
                    self._pending_resumes.add(doc.interview_id)
                    interrupted_at = datetime.utcnow()

                    # Estimate where in the text we were interrupted.
                    # audio_buffer contains chunks generated (and played) up to interrupt.
                    # Ratio of played bytes to total estimated bytes ≈ ratio of chars heard.
                    interrupted_at_char: int | None = None
                    try:
                        played_bytes = sum(len(c) for c in audio_buffer) if 'audio_buffer' in dir() else (
                            len(doc.audio_data) if doc.audio_data else 0
                        )
                        # Rough estimate: Edge TTS s16le 22050Hz mono → ~44100 bytes/sec
                        # Total expected bytes ≈ len(text) chars × ~150wpm × 60s/min × 44100 bytes/s
                        # Simpler: if we have the full synthesized audio, ratio gives a good estimate
                        total_bytes = len(doc.audio_data) if doc.audio_data else (
                            sum(len(c) for c in audio_buffer) * 2  # assume ~50% through if no full data
                        )
                        if total_bytes > 0 and played_bytes > 0:
                            ratio = min(played_bytes / total_bytes, 1.0)
                            interrupted_at_char = int(ratio * len(doc.text))
                    except Exception:
                        pass

                    asyncio.create_task(
                        self._maybe_resume(doc.interview_id, doc.text, interrupted_at,
                                           interrupted_at_char=interrupted_at_char)
                    )
                elif is_resume:
                    logger.info(
                        f"Resumed transcript also interrupted — not re-scheduling "
                        f"(silence timeout will handle if candidate says nothing): {doc.id}"
                    )

        except Exception as exc:
            logger.error(f"Transcript processing failed: {doc.id} — {exc}")
            try:
                await self._transcript_updater.mark_played(doc.id, None)
            except Exception as update_exc:
                logger.error(f"Failed to mark transcript as played: {update_exc}")

        finally:
            # Disarm: mark bot as no longer speaking, cancel change stream watcher
            interrupted = self._interrupt_handler.stop_event.is_set()
            await self._interrupt_handler.disarm(doc.interview_id)
            await self._set_tts_status(doc.interview_id, "idle", interrupted=interrupted)

    async def _maybe_resume(self, interview_id: str, text: str, interrupted_at: datetime,
                             interrupted_at_char: int | None = None) -> None:
        """
        After a TTS interrupt, wait 45s for the candidate to say something
        meaningful. If no new agent transcript appears (meaning the pipeline
        never fired), re-insert the interrupted text so TTS plays it again.
        Prevents the conversation deadlocking when a false/brief interrupt
        fires but the candidate had nothing to say.

        interrupted_at_char: approximate character offset in `text` where audio
        stopped. Included in metadata so Main-Agent can generate a contextual
        re-entry ("To continue from where I was...") instead of a verbatim replay.

        45s wait accounts for LLM call latency (Gemini can take 10-20s).
        """
        RESUME_WAIT_SEC = 45.0  # covers slow Gemini calls (10-20s) + retries + DB latency
        await asyncio.sleep(RESUME_WAIT_SEC)
        try:
            db = self._mongo_client[config.mongodb_db]
            col = db[config.transcripts_collection]

            # Use pre-sleep timestamp (captured before the 12s wait)
            # If candidate spoke, the pipeline will handle it — don't resume
            new_candidate = await col.find_one({
                "interview_id": interview_id,
                "speaker": "candidate",
                "timestamp": {"$gt": interrupted_at},
            })
            if new_candidate:
                logger.info(f"Resume check: candidate replied — skip resume [{interview_id}]")
                return

            # Check if a new agent transcript was inserted after the interrupt
            new_agent = await col.find_one({
                "interview_id": interview_id,
                "speaker": "agent",
                "timestamp": {"$gt": interrupted_at},
            })
            if new_agent:
                logger.info(f"Resume check: pipeline responded — skip resume [{interview_id}]")
                return

            # Don't resume if interview ended
            interview = await db.interviews.find_one(
                {"interview_id": interview_id},
                projection={"status": 1},
            )
            if interview and interview.get("status") in ("completed", "abandoned"):
                return

            # No response and interview still active — re-play interrupted text.
            # Include interrupted_at_char so Main-Agent can generate a natural
            # re-entry ("To continue from where I was...") using the context.
            logger.info(
                f"Resume: no candidate reply after {RESUME_WAIT_SEC}s "
                f"— resuming interrupted response [{interview_id}]"
                + (f" (interrupted at char {interrupted_at_char})" if interrupted_at_char else "")
            )
            resume_metadata: dict = {"resumed_after_interrupt": True}
            if interrupted_at_char is not None:
                resume_metadata["interrupted_at_char"] = interrupted_at_char
                # Include the tail of what was unsaid so Main-Agent can incorporate it
                resume_metadata["unsaid_text"] = text[interrupted_at_char:].strip()
            await col.insert_one({
                "interview_id": interview_id,
                "speaker": "agent",
                "text": text,
                "audio_url": None,
                "timestamp": datetime.utcnow(),
                "metadata": resume_metadata,
            })
        except Exception as exc:
            logger.error(f"Resume check failed [{interview_id}]: {exc}")
        finally:
            self._pending_resumes.discard(interview_id)

    async def stop(self) -> None:
        """Stop TTS service gracefully."""
        logger.info("Stopping TTS Service...")
        self._running = False
        await self._interrupt_handler.close()
        await self._transcript_listener.close()
        await self._synthesizer.close()
        self._mongo_client.close()
        logger.info("TTS Service stopped")


async def main() -> None:
    """Async entry point."""
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    service = TTSService()

    loop = asyncio.get_event_loop()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        asyncio.create_task(service.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await service.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await service.stop()


def main_sync() -> None:
    """Synchronous entry point for uv script."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
