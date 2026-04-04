"""MongoDB transcript update helper — stores audio in GridFS."""
import logging
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket

from .config import config

logger = logging.getLogger(__name__)


class TranscriptUpdater:
    """Updates interviews.transcripts; stores audio in GridFS."""

    def __init__(self, mongo_client: AsyncIOMotorClient) -> None:
        self.db = mongo_client[config.mongodb_db]
        self.collection = self.db[config.transcripts_collection]
        self._bucket = AsyncIOMotorGridFSBucket(self.db, bucket_name="audio")

    async def mark_played(
        self, document_id: Any, audio_data: Optional[bytes] = None
    ) -> None:
        """
        Upload PCM audio to GridFS and set audio_url to the GridFS file ID.
        If no audio_data, sets audio_url='played' as a fallback signal.

        Document stays clean — no extra fields added to the transcript.
        """
        if audio_data:
            file_id = await self._bucket.upload_from_stream(
                f"transcript_{document_id}.pcm",
                audio_data,
                metadata={
                    "transcript_id": str(document_id),
                    "format": "pcm_22050",
                    "sample_rate": 22050,
                    "channels": 1,
                    "bits": 16,
                },
            )
            audio_url = str(file_id)
            logger.info(
                f"Audio stored in GridFS: {file_id} "
                f"({len(audio_data) / 1024:.1f} KB)"
            )
        else:
            audio_url = "played"

        await self.collection.update_one(
            {"_id": document_id},
            {"$set": {"audio_url": audio_url}},
        )
        logger.info(f"Transcript updated: {document_id} → audio_url={audio_url}")
