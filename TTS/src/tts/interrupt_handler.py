"""MongoDB-based interrupt handler.

Replaces the NATS-based interrupt handler. Uses a MongoDB change stream on
interviews.interviews to detect when Meeting-Bot sets tts_interrupt=True on the
active interview (because the candidate started speaking while the bot was talking).

Flow:
  1. TTS calls arm(interview_id) before starting audio playback.
     - Clears stop_event
     - Sets bot_speaking=True, tts_interrupt=False on the interview document
     - Starts a background change stream task watching for tts_interrupt=True
     - Blocks interrupts for first 500ms (bot always completes first 2-3 words)
  2. AudioPlayer checks stop_event on every PCM chunk. When set, pacat is killed.
  3. Meeting-Bot sets tts_interrupt=True when a caption arrives while bot is speaking.
     - Change stream fires -> stop_event.set() -> audio stops mid-stream.
  4. TTS calls disarm(interview_id) after audio finishes (naturally or interrupted).
     - Cancels the watch task
     - Sets bot_speaking=False on the interview document
"""
import asyncio
import logging
import time
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

from .config import config

logger = logging.getLogger(__name__)

# Interrupt block window - bot always completes first 500ms of speech (2-3 words)
INTERRUPT_BLOCK_WINDOW_SEC = 0.5


class InterruptHandler:
    """Watches interviews.interviews for tts_interrupt flag via MongoDB change stream."""

    def __init__(self, mongo_client: AsyncIOMotorClient) -> None:
        self.db = mongo_client[config.mongodb_db]
        self._stop_event = asyncio.Event()
        self._watch_task: Optional[asyncio.Task] = None
        self._armed_at: float = 0.0
        self._block_until: float = 0.0  # Timestamp until which interrupts are blocked

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop_event

    async def arm(self, interview_id: str) -> None:
        """
        Prepare for a new utterance:
        - Clear stop_event so audio plays uninterrupted until an interrupt arrives.
        - Set bot_speaking=True, tts_interrupt=False on the interview document
          (clears any stale interrupt flag from the previous turn).
        - Start the background change stream watcher for this interview.
        - Block interrupts for first 500ms (bot always completes first 2-3 words).
        """
        self._stop_event.clear()

        await self.db.interviews.update_one(
            {"interview_id": interview_id},
            {"$set": {"bot_speaking": True, "tts_interrupt": False}},
        )

        # Cancel any previous watch before starting a fresh one
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass

        self._armed_at = asyncio.get_event_loop().time()
        # Block interrupts for first 500ms - bot always completes first 2-3 words
        self._block_until = self._armed_at + INTERRUPT_BLOCK_WINDOW_SEC
        
        self._watch_task = asyncio.create_task(self._watch(interview_id))
        logger.info(f"Interrupt handler armed: interview={interview_id} (blocked until {self._block_until - self._armed_at:.3f}s)")

    async def disarm(self, interview_id: str) -> None:
        """
        Called after audio finishes (naturally or interrupted):
        - Cancel the change stream watcher.
        - Set bot_speaking=False so Meeting-Bot stops sending interrupt signals.
        """
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass

        await self.db.interviews.update_one(
            {"interview_id": interview_id},
            {"$set": {"bot_speaking": False}},
        )
        logger.info(f"Interrupt handler disarmed: interview={interview_id}")

    async def _watch(self, interview_id: str) -> None:
        """Background task: change stream on interviews.interviews.

        Fires when tts_interrupt becomes True on the given interview.
        Sets stop_event so AudioPlayer stops mid-stream.
        Blocks interrupts for first 500ms to let bot complete first 2-3 words.

        Reconnects on transient MongoDB errors — a single exception must NOT
        permanently disable interruptions for the rest of the audio session.
        """
        pipeline = [
            {
                "$match": {
                    "operationType": "update",
                    "updateDescription.updatedFields.tts_interrupt": True,
                }
            }
        ]
        retry_delay = 0.5
        while True:
            try:
                async with self.db.interviews.watch(
                    pipeline, full_document="updateLookup"
                ) as stream:
                    retry_delay = 0.5  # reset on successful open
                    async for change in stream:
                        full_doc = change.get("fullDocument") or {}
                        if full_doc.get("interview_id") == interview_id:
                            elapsed = asyncio.get_event_loop().time() - self._armed_at

                            # Honour the block window so bot speaks at least 2-3 words
                            if elapsed < INTERRUPT_BLOCK_WINDOW_SEC:
                                logger.debug(
                                    f"Interrupt blocked (first {INTERRUPT_BLOCK_WINDOW_SEC}s): "
                                    f"interview={interview_id}"
                                )
                                await asyncio.sleep(INTERRUPT_BLOCK_WINDOW_SEC - elapsed)

                            logger.info(
                                f"Interrupt received: interview={interview_id} — stopping audio"
                            )
                            self._stop_event.set()
                            return  # One interrupt per utterance — done
            except asyncio.CancelledError:
                return  # Normal — disarm() cancelled after audio finished
            except Exception as exc:
                if self._stop_event.is_set():
                    # Audio already finished/interrupted — no need to reconnect
                    return
                logger.warning(
                    f"Interrupt watch error (retry in {retry_delay:.1f}s): {exc}"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 10.0)

    async def close(self) -> None:
        """Cancel any running watch task on service shutdown."""
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
