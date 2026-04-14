"""TTS interrupt handler — Redis-first optimistic interruption.

Flow (Redis path, ~50–200ms total):
  1. Meeting-Bot publishes tts:{id}:interrupt_signal on onspeechstart (any audio).
  2. TTS starts a 400ms countdown.
  3a. If tts:{id}:interrupt_confirm arrives before 400ms → stop audio (~50ms total).
  3b. If 400ms expires with no confirm → publish tts:{id}:interrupt_cancel → resume seamlessly.

Fallback (MongoDB path, 500ms–2s):
  If Redis is unavailable, falls back to watching tts_interrupt flag via MongoDB change stream.
  No regression from current behaviour.
"""
import asyncio
import logging
import time
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

from .config import config

logger = logging.getLogger(__name__)

# Interrupt block window — bot always completes first 500ms of speech (2-3 words).
INTERRUPT_BLOCK_WINDOW_SEC = 0.5
# Optimistic countdown: if no confirm arrives within this window, treat as noise and cancel.
INTERRUPT_CONFIRM_WINDOW_SEC = 0.4


class InterruptHandler:
    """Redis-first optimistic interrupt handler with MongoDB fallback."""

    def __init__(self, mongo_client: AsyncIOMotorClient) -> None:
        self.db = mongo_client[config.mongodb_db]
        self._stop_event = asyncio.Event()
        self._watch_task: Optional[asyncio.Task] = None
        self._armed_at: float = 0.0

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop_event

    async def arm(self, interview_id: str) -> None:
        """Prepare for a new utterance.

        Clears stop_event, sets bot_speaking=True / tts_interrupt=False in MongoDB,
        then starts the interrupt watcher (Redis-first, MongoDB fallback).
        Blocks all interrupts for the first 500ms so bot speaks at least 2-3 words.
        """
        self._stop_event.clear()

        await self.db.interviews.update_one(
            {"interview_id": interview_id},
            {"$set": {"bot_speaking": True, "tts_interrupt": False}},
        )

        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass

        self._armed_at = asyncio.get_event_loop().time()
        self._watch_task = asyncio.create_task(self._watch(interview_id))
        logger.info(f"Interrupt handler armed: interview={interview_id}")

    async def disarm(self, interview_id: str) -> None:
        """Called after audio finishes (naturally or interrupted)."""
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
        """Try Redis-first; fall back to MongoDB change stream if Redis unavailable."""
        try:
            from .redis_client import get_redis
            redis = await get_redis()
            await self._watch_redis(interview_id, redis)
        except Exception as redis_err:
            logger.warning(
                f"Redis interrupt handler unavailable ({redis_err}), "
                f"falling back to MongoDB for interview={interview_id}"
            )
            await self._watch_mongo(interview_id)

    # ─── Redis path (optimistic: fire-first, cancel-if-noise) ────────────────

    async def _watch_redis(self, interview_id: str, redis) -> None:
        """Subscribe to interrupt_signal channel. On signal, start 400ms countdown.

        If interrupt_confirm arrives within 400ms → stop audio.
        If 400ms expires with no confirm → publish interrupt_cancel and resume.
        Deduplicates rapid signals within the countdown window.
        """
        signal_channel = f"tts:{interview_id}:interrupt_signal"
        confirm_channel = f"tts:{interview_id}:interrupt_confirm"

        pubsub = redis.pubsub()
        await pubsub.subscribe(signal_channel, confirm_channel)
        logger.debug(f"Redis interrupt: subscribed to {signal_channel}, {confirm_channel}")

        countdown_task: Optional[asyncio.Task] = None
        in_countdown = False

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue

                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()

                if channel == signal_channel:
                    if in_countdown:
                        logger.debug(
                            f"Redis interrupt: duplicate signal ignored (countdown active): "
                            f"interview={interview_id}"
                        )
                        continue

                    elapsed = asyncio.get_event_loop().time() - self._armed_at
                    if elapsed < INTERRUPT_BLOCK_WINDOW_SEC:
                        logger.debug(
                            f"Redis interrupt: signal blocked (first {INTERRUPT_BLOCK_WINDOW_SEC}s): "
                            f"interview={interview_id}"
                        )
                        continue

                    logger.info(
                        f"Redis interrupt: signal received — starting {INTERRUPT_CONFIRM_WINDOW_SEC*1000:.0f}ms countdown: "
                        f"interview={interview_id}"
                    )
                    in_countdown = True
                    if countdown_task and not countdown_task.done():
                        countdown_task.cancel()
                    countdown_task = asyncio.create_task(
                        self._countdown_then_cancel(interview_id, redis, pubsub)
                    )

                elif channel == confirm_channel:
                    if not in_countdown:
                        continue

                    logger.info(
                        f"Redis interrupt: confirm received — stopping audio: "
                        f"interview={interview_id}"
                    )
                    if countdown_task and not countdown_task.done():
                        countdown_task.cancel()

                    self._stop_event.set()
                    return  # One interrupt per utterance — done

        except asyncio.CancelledError:
            if countdown_task and not countdown_task.done():
                countdown_task.cancel()
        finally:
            try:
                await pubsub.unsubscribe(signal_channel, confirm_channel)
            except Exception:
                pass

    async def _countdown_then_cancel(self, interview_id: str, redis, pubsub) -> None:
        """Wait INTERRUPT_CONFIRM_WINDOW_SEC. If no confirm arrives, publish cancel."""
        try:
            await asyncio.sleep(INTERRUPT_CONFIRM_WINDOW_SEC)
            # No confirm — treat as noise, publish cancel so Meeting-Bot clears accumulation
            cancel_channel = f"tts:{interview_id}:interrupt_cancel"
            try:
                await redis.publish(cancel_channel, "cancel")
                logger.info(
                    f"Redis interrupt: no confirm in {INTERRUPT_CONFIRM_WINDOW_SEC*1000:.0f}ms "
                    f"— published cancel: interview={interview_id}"
                )
            except Exception as e:
                logger.warning(f"Redis interrupt: failed to publish cancel: {e}")
        except asyncio.CancelledError:
            pass  # Confirm arrived — countdown superseded

    # ─── MongoDB fallback path ────────────────────────────────────────────────

    async def _watch_mongo(self, interview_id: str) -> None:
        """MongoDB change stream fallback. Identical logic to original handler."""
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
                    retry_delay = 0.5

                    try:
                        current = await self.db.interviews.find_one(
                            {"interview_id": interview_id},
                            projection={"tts_interrupt": 1},
                        )
                        if current and current.get("tts_interrupt"):
                            elapsed = asyncio.get_event_loop().time() - self._armed_at
                            if elapsed < INTERRUPT_BLOCK_WINDOW_SEC:
                                await asyncio.sleep(INTERRUPT_BLOCK_WINDOW_SEC - elapsed)
                            logger.info(
                                f"MongoDB interrupt (post-open check): interview={interview_id}"
                            )
                            self._stop_event.set()
                            return
                    except Exception as check_err:
                        logger.warning(f"Post-open interrupt check failed (continuing): {check_err}")

                    async for change in stream:
                        full_doc = change.get("fullDocument") or {}
                        if full_doc.get("interview_id") == interview_id:
                            elapsed = asyncio.get_event_loop().time() - self._armed_at
                            if elapsed < INTERRUPT_BLOCK_WINDOW_SEC:
                                await asyncio.sleep(INTERRUPT_BLOCK_WINDOW_SEC - elapsed)
                            logger.info(
                                f"MongoDB interrupt received: interview={interview_id}"
                            )
                            self._stop_event.set()
                            return

            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._stop_event.is_set():
                    return
                logger.warning(
                    f"MongoDB interrupt watch error (retry in {retry_delay:.1f}s): {exc}"
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
