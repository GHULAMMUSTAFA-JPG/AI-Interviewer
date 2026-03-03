"""TTS Service Configuration"""
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(usecwd=True))  # searches up to root .env for local dev


@dataclass(frozen=True)
class Config:
    """Immutable configuration loaded from environment variables."""

    # MongoDB
    mongodb_uri: str = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    mongodb_db: str = os.getenv("MONGODB_DB", "interviews")
    transcripts_collection: str = os.getenv("TRANSCRIPTS_COLLECTION", "transcripts")

    # Audio device (optional — leave blank to use system default)
    audio_device_id: Optional[int] = (
        int(os.getenv("AUDIO_DEVICE_ID"))
        if os.getenv("AUDIO_DEVICE_ID")
        else None
    )
    pulse_server: str = os.getenv("PULSE_SERVER", "unix:/run/user/1000/pulse/native")
    virtual_mic: str = os.getenv("VIRTUAL_MIC", "virtual_mic")

    # ElevenLabs
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
    elevenlabs_voice_id: str = os.getenv(
        "ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"
    )
    elevenlabs_model_id: str = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    elevenlabs_output_format: str = os.getenv(
        "ELEVENLABS_OUTPUT_FORMAT", "pcm_22050"
    )
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    elevenlabs_connect_timeout: float = 10.0  # seconds

    # Audio
    sample_rate: int = 22050
    channels: int = 1
    dtype: str = "int16"
    chunk_size: int = 4096  # bytes

    # Performance
    interruption_latency_ms: int = 50

    # Reconnection
    mongo_max_retries: int = 5
    mongo_retry_base_delay: float = 1.0  # seconds


config = Config()
