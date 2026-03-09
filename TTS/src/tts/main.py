"""TTS Service entry point."""
import asyncio
import logging
import os
import signal
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClient

from .config import config
from .logging_config import setup_logging
from .tts_queue import TranscriptListener
from .synthesizer import ElevenLabsSynthesizer
from .audio_player import AudioPlayer
from .interrupt_handler import InterruptHandler
from .status_updater import TranscriptUpdater
from models.tts_queue import TranscriptDocument

logger = logging.getLogger(__name__)


class TTSService:
    """Main TTS service orchestrator."""

    def __init__(self) -> None:
        self._mongo_client = AsyncIOMotorClient(config.mongodb_uri)
        self._transcript_listener = TranscriptListener(self._mongo_client)
        self._synthesizer = ElevenLabsSynthesizer()
        self._player = AudioPlayer()
        self._interrupt_handler = InterruptHandler(self._mongo_client)
        self._transcript_updater = TranscriptUpdater(self._mongo_client)
        self._running = False
        # Tracks interviews that already have a pending resume to prevent cascade
        self._pending_resumes: set[str] = set()

    async def start(self) -> None:
        """Start TTS service."""
        logger.info("Starting TTS Service (MongoDB interrupt mode)...")
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

        # Arm: mark bot as speaking, clear stale interrupt, start watching
        await self._interrupt_handler.arm(doc.interview_id)

        try:
            if doc.audio_data:
                # --- Cache hit: play from DB, no ElevenLabs call ---
                logger.info(
                    f"Playing from DB cache: {doc.id} "
                    f"({len(doc.audio_data) / 1024:.1f} KB)"
                )
                await self._player.play(
                    self._chunks_from_bytes(doc.audio_data),
                    self._interrupt_handler.stop_event,
                )
                await self._transcript_updater.mark_played(doc.id, doc.audio_data)
            else:
                # --- Cache miss: synthesize from ElevenLabs ---
                audio_buffer: list[bytes] = []
                raw_chunks = self._synthesizer.synthesize(doc.text)
                teed = self._buffered(raw_chunks, audio_buffer)

                try:
                    await self._player.play(teed, self._interrupt_handler.stop_event)
                except Exception as audio_exc:
                    # Audio device unavailable (e.g. inside a container without PulseAudio) —
                    # drain the generator so the buffer is still filled
                    logger.warning(
                        f"Audio playback unavailable: {audio_exc} — synthesizing to DB only"
                    )
                    async for _ in teed:
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
                is_resume = getattr(doc, "metadata", None) and (
                    doc.metadata or {}
                ).get("resumed_after_interrupt")
                if not is_resume and doc.interview_id not in self._pending_resumes:
                    self._pending_resumes.add(doc.interview_id)
                    interrupted_at = datetime.utcnow()
                    asyncio.create_task(
                        self._maybe_resume(doc.interview_id, doc.text, interrupted_at)
                    )

        except Exception as exc:
            logger.error(f"Transcript processing failed: {doc.id} — {exc}")
            try:
                await self._transcript_updater.mark_played(doc.id, None)
            except Exception as update_exc:
                logger.error(f"Failed to mark transcript as played: {update_exc}")

        finally:
            # Disarm: mark bot as no longer speaking, cancel change stream watcher
            await self._interrupt_handler.disarm(doc.interview_id)

    async def _maybe_resume(self, interview_id: str, text: str, interrupted_at: datetime) -> None:
        """
        After a TTS interrupt, wait 12s for the candidate to say something
        meaningful. If no new agent transcript appears (meaning the pipeline
        never fired), re-insert the interrupted text so TTS plays it again.
        Prevents the conversation deadlocking when a false/brief interrupt
        fires but the candidate had nothing to say.
        """
        RESUME_WAIT_SEC = 12.0
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

            # No response and interview still active — re-play interrupted text
            logger.info(
                f"Resume: no candidate reply after {RESUME_WAIT_SEC}s "
                f"— resuming interrupted response [{interview_id}]"
            )
            await col.insert_one({
                "interview_id": interview_id,
                "speaker": "agent",
                "text": text,
                "audio_url": None,
                "timestamp": datetime.utcnow(),
                "metadata": {"resumed_after_interrupt": True},
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
