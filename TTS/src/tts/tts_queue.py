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
        5 minutes that TTS may have missed during a restart.

        5-minute window (down from 30) avoids double-playing a greeting from
        an interview that started long ago when TTS restarts in a new session.
        Transcripts older than 5 min that were never played are likely from a
        crashed/abandoned interview and should not be replayed.

        Called once at startup before the change stream opens.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=5)
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
            logger.info(f"Backfill: replaying {count} missed transcript(s) from last 5 min")
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

        # Retry forever — in production a transient MongoDB blip should not
        # permanently stop TTS from playing agent responses.
        while not self._closed:
            try:
                self._change_stream = self.collection.watch(
                    pipeline,
                    full_document="updateLookup",
                )
                logger.info(
                    f"Watching {config.mongodb_db}.{config.transcripts_collection} "
                    "for agent utterances (change stream open)"
                )
                retry_delay = config.mongo_retry_base_delay  # reset on successful open

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

            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._closed:
                    return
                logger.warning(
                    f"MongoDB change stream error (retry in {retry_delay:.1f}s): {exc}"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)  # exponential backoff, cap 30s

    async def close(self) -> None:
        """Close change stream."""
        self._closed = True
        if self._change_stream:
            await self._change_stream.close()
            logger.info("MongoDB change stream closed")
