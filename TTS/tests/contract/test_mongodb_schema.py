"""Contract tests: MongoDB document schema validation."""
import pytest
from datetime import datetime
from models.tts_queue import TTSQueueDocument, TTSStatus


class TestMongoDBSchema:
    """Validate TTSQueueDocument schema matches MongoDB contract."""

    def test_create_minimal_document(self):
        doc = TTSQueueDocument(session_id="sess_001", text="Hello world")
        assert doc.session_id == "sess_001"
        assert doc.text == "Hello world"
        assert doc.status == TTSStatus.PENDING
        assert doc.priority == "normal"
        assert doc.id is not None
        assert doc.started_at is None
        assert doc.completed_at is None
        assert doc.interrupted_at is None
        assert doc.error_message is None

    def test_document_id_is_string(self):
        doc = TTSQueueDocument(session_id="sess_001", text="Test")
        assert isinstance(doc.id, str)

    def test_status_enum_values(self):
        assert TTSStatus.PENDING.value == "pending"
        assert TTSStatus.PLAYING.value == "playing"
        assert TTSStatus.COMPLETED.value == "completed"
        assert TTSStatus.INTERRUPTED.value == "interrupted"
        assert TTSStatus.FAILED.value == "failed"

    def test_document_status_transition(self):
        doc = TTSQueueDocument(session_id="sess_001", text="Test")
        assert doc.status == TTSStatus.PENDING
        updated = doc.model_copy(update={"status": TTSStatus.PLAYING})
        assert updated.status == TTSStatus.PLAYING

    def test_document_with_timestamps(self):
        now = datetime.utcnow()
        doc = TTSQueueDocument(
            session_id="sess_001",
            text="Test",
            status=TTSStatus.COMPLETED,
            started_at=now,
            completed_at=now,
        )
        assert doc.started_at == now
        assert doc.completed_at == now

    def test_document_with_error(self):
        doc = TTSQueueDocument(
            session_id="sess_001",
            text="Test",
            status=TTSStatus.FAILED,
            error_message="Piper crashed",
        )
        assert doc.error_message == "Piper crashed"

    def test_alias_id_field(self):
        """Document can be constructed with _id alias (MongoDB format)."""
        doc = TTSQueueDocument(
            **{"_id": "custom-id", "session_id": "sess_001", "text": "Test"}
        )
        assert doc.id == "custom-id"

    def test_priority_values(self):
        doc_normal = TTSQueueDocument(session_id="s", text="t", priority="normal")
        doc_high = TTSQueueDocument(session_id="s", text="t", priority="high")
        assert doc_normal.priority == "normal"
        assert doc_high.priority == "high"
