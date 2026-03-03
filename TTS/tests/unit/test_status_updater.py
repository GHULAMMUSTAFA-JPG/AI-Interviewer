"""Unit tests: TranscriptUpdater MongoDB writes."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId


class TestTranscriptUpdater:
    """Tests for TranscriptUpdater MongoDB operations."""

    @pytest.fixture
    def updater(self):
        from src.tts.status_updater import TranscriptUpdater

        client = MagicMock()
        collection = AsyncMock()
        db = MagicMock()
        db.__getitem__ = MagicMock(return_value=collection)
        client.__getitem__ = MagicMock(return_value=db)

        # Patch GridFSBucket so it doesn't require a real Motor database
        mock_bucket = AsyncMock()
        mock_bucket.upload_from_stream = AsyncMock(return_value=ObjectId())
        with patch("src.tts.status_updater.AsyncIOMotorGridFSBucket", return_value=mock_bucket):
            updater = TranscriptUpdater(client)

        updater.collection = collection
        updater._bucket = mock_bucket
        return updater, collection, mock_bucket

    @pytest.mark.asyncio
    async def test_mark_played_no_audio_sets_played(self, updater):
        """Without audio_data, audio_url is set to 'played'."""
        inst, collection, _ = updater
        collection.update_one = AsyncMock()

        await inst.mark_played("doc-001", None)

        call_args = collection.update_one.call_args
        assert call_args[0][1]["$set"]["audio_url"] == "played"

    @pytest.mark.asyncio
    async def test_mark_played_with_audio_stores_to_gridfs(self, updater):
        """With audio_data, audio is uploaded to GridFS."""
        inst, collection, bucket = updater
        collection.update_one = AsyncMock()
        fake_id = ObjectId()
        bucket.upload_from_stream = AsyncMock(return_value=fake_id)

        await inst.mark_played("doc-001", b"\x00" * 100)

        bucket.upload_from_stream.assert_called_once()
        call_args = collection.update_one.call_args
        assert call_args[0][1]["$set"]["audio_url"] == str(fake_id)

    @pytest.mark.asyncio
    async def test_mark_played_uses_correct_document_id(self, updater):
        """mark_played() filters by _id."""
        inst, collection, _ = updater
        collection.update_one = AsyncMock()

        await inst.mark_played("my-doc-id", None)

        filter_doc = collection.update_one.call_args[0][0]
        assert filter_doc["_id"] == "my-doc-id"

    @pytest.mark.asyncio
    async def test_mark_played_accepts_objectid(self, updater):
        """mark_played() works with ObjectId as document id."""
        inst, collection, _ = updater
        collection.update_one = AsyncMock()

        oid = ObjectId()
        await inst.mark_played(oid, None)

        filter_doc = collection.update_one.call_args[0][0]
        assert filter_doc["_id"] == oid
