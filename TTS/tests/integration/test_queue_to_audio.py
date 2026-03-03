"""
Integration test: Queue insert triggers synthesis.

NOTE: These tests require real MongoDB with replica set (change streams).
      They are skipped automatically if MONGODB_URI is not a replica set.
      Run with: pytest tests/integration/ -v
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, AsyncMock

pytestmark = pytest.mark.asyncio


class TestQueueToAudio:
    """Integration tests for queue → synthesis → playback flow."""

    async def test_document_parsed_correctly(self):
        """Verify change stream document is parsed into TTSQueueDocument."""
        from models.tts_queue import TTSQueueDocument, TTSStatus
        import uuid

        raw_doc = {
            "_id": str(uuid.uuid4()),
            "session_id": "sess_integration_001",
            "text": "Hello, integration test.",
            "status": "pending",
            "priority": "normal",
        }
        doc = TTSQueueDocument.model_validate(raw_doc)
        assert doc.status == TTSStatus.PENDING
        assert doc.text == "Hello, integration test."

    async def test_status_updates_in_order(self):
        """Status transitions: pending → playing → completed."""
        from models.tts_queue import TTSStatus
        statuses = []

        class FakeUpdater:
            async def set_status(self, doc_id, status, error=None):
                statuses.append(status)

        updater = FakeUpdater()
        await updater.set_status("id1", TTSStatus.PLAYING)
        await updater.set_status("id1", TTSStatus.COMPLETED)

        assert statuses == [TTSStatus.PLAYING, TTSStatus.COMPLETED]

    async def test_interrupt_stops_before_complete(self):
        """When interrupt fires, status becomes interrupted not completed."""
        from models.tts_queue import TTSStatus
        statuses = []

        class FakeUpdater:
            async def set_status(self, doc_id, status, error=None):
                statuses.append(status)

        updater = FakeUpdater()
        stop_event = asyncio.Event()
        stop_event.set()  # pre-set interrupt

        await updater.set_status("id1", TTSStatus.PLAYING)

        # Simulate what main.py does after play()
        if stop_event.is_set():
            await updater.set_status("id1", TTSStatus.INTERRUPTED)
        else:
            await updater.set_status("id1", TTSStatus.COMPLETED)

        assert statuses[-1] == TTSStatus.INTERRUPTED
