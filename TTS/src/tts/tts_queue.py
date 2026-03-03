"""MongoDB change stream listener for interviews.transcripts."""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import AsyncGenerator

from motor.motor_asyncio import AsyncIOMotorClient

from .config import config
from models.tts_queue import TranscriptDocument

logger = logging.getLogger(__name__)


class TranscriptListener:
    """Watches interviews.transcripts for agent utterances to synthesize."""

    def __init__(self, mongo_client: AsyncIOMotorClient) -> None:
        self.db = mongo_client[config.mongodb_db]
        self.collection = self.db[config.transcripts_collection]
        self._change_stream = None
        self._closed = False

    async def backfill(self) -> AsyncGenerator[TranscriptDocument, None]:
        """
        Yield any agent transcripts with audio_url=null inserted in the last
        30 minutes that TTS may have missed during a restart.

        Called once at startup before the change stream opens so that
        transcripts queued while TTS was down are not permanently skipped.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=30)
        cursor = self.collection.find(
            {
                "speaker": "agent",
                "audio_url": None,
                "timestamp": {"$gte": cutoff},
            },
            sort=[("timestamp", 1)],
        )
        count = 0
        async for raw in cursor:
            try:
                doc = TranscriptDocument.model_validate(raw)
                count += 1
                yield doc
            except Exception as exc:
                logger.error(f"Backfill parse error: {exc}")
        if count:
            logger.info(f"Backfill: replayed {count} missed transcript(s)")
        else:
            logger.info("Backfill: no missed transcripts found")

    async def watch(self) -> AsyncGenerator[TranscriptDocument, None]:
        """
        Watch for new agent transcript documents without audio_url.

        Yields TranscriptDocument on insert where speaker=agent and audio_url=null.
        Reconnects automatically on transient errors.
        """
        pipeline = [
            {
                "$match": {
                    "operationType": "insert",
                    "fullDocument.speaker": "agent",
                    "fullDocument.audio_url": None,
                }
            }
        ]
        retry_delay = config.mongo_retry_base_delay
        max_retries = config.mongo_max_retries

        for attempt in range(max_retries):
            try:
                self._change_stream = self.collection.watch(
                    pipeline,
                    full_document="updateLookup",
                )
                logger.info(
                    f"Watching {config.mongodb_db}.{config.transcripts_collection} "
                    "for agent utterances"
                )
                retry_delay = config.mongo_retry_base_delay  # reset on success

                async for change in self._change_stream:
                    if self._closed:
                        return
                    try:
                        full_doc = change.get("fullDocument")
                        if not full_doc:
                            continue
                        doc = TranscriptDocument.model_validate(full_doc)
                        logger.info(
                            f"New transcript: interview={doc.interview_id} "
                            f"chars={len(doc.text)}"
                        )
                        yield doc
                    except Exception as exc:
                        logger.error(f"Error parsing transcript document: {exc}")

            except Exception as exc:
                if self._closed:
                    return
                logger.warning(
                    f"MongoDB change stream error "
                    f"(attempt {attempt + 1}/{max_retries}): {exc}"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)  # exponential backoff

        logger.error("Max MongoDB reconnection attempts reached")

    async def close(self) -> None:
        """Close change stream."""
        self._closed = True
        if self._change_stream:
            await self._change_stream.close()
            logger.info("MongoDB change stream closed")
