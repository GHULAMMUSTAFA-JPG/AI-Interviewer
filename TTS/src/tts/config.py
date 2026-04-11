"""TTS Service Configuration — Edge TTS branch (free, no API key)."""
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
    # TTS audio must play to virtual_mic (not VirtualSink) so it flows:
    #   virtual_mic → virtual_mic.monitor → virtual_mic_source → Chrome WebRTC mic
    # Writing to VirtualSink instead would loop bot audio back through BotMic into STT.
    virtual_mic: str = os.getenv("VIRTUAL_MIC", "virtual_mic")

    # Edge TTS (Microsoft) — free, no API key required
    edge_tts_voice: str = os.getenv("EDGE_TTS_VOICE", "en-US-GuyNeural")

    # Audio
    sample_rate: int = 22050
    channels: int = 1
    dtype: str = "int16"
    chunk_size: int = 4096  # bytes

    # PulseAudio player buffer — 30 ms is the sweet spot: low enough for snappy
    # interruption, high enough that pacat never underruns on a loaded system.
    pacat_latency_msec: int = int(os.getenv("PACAT_LATENCY_MSEC", "30"))

    # Performance
    interruption_latency_ms: int = 50

    # Reconnection
    mongo_max_retries: int = 5
    mongo_retry_base_delay: float = 1.0  # seconds


config = Config()
