"""Pydantic models for TTS queue documents from MongoDB."""
from typing import Optional, Any
from pydantic import BaseModel, Field, field_validator


class TranscriptDocument(BaseModel):
    """Represents one row from interviews.transcripts that TTS needs to speak."""

    id: Any = Field(alias="_id")
    interview_id: str
    speaker: str
    text: str
    audio_url: Optional[str] = None
    # audio_data: populated by callers that pre-load GridFS bytes (backfill replay).
    # None when coming straight from the change stream.
    audio_data: Optional[bytes] = None

    model_config = {"populate_by_name": True}
