"""Edge TTS synthesizer — free, no API key required.

Uses Microsoft's Edge browser's built-in TTS service via the edge-tts library.
Produces PCM audio compatible with the existing AudioPlayer (pacat playback).

Voice list: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts#edge-tts
Recommended voices:
  - en-US-GuyNeural (male, natural)
  - en-US-JennyNeural (female, natural)
  - en-GB-RyanNeural (British male)
  - en-GB-SoniaNeural (British female)
"""
import asyncio
import logging
import struct
from typing import AsyncGenerator

import edge_tts

from .config import config

logger = logging.getLogger(__name__)


class EdgeTTSSynthesizer:
    """Generates PCM audio via Microsoft Edge TTS (free, no API key)."""

    def __init__(self):
        self._voice = config.edge_tts_voice
        logger.info(f"EdgeTTSSynthesizer initialized with voice={self._voice}")

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Synthesize text to PCM audio chunks via Edge TTS.

        Yields raw 16-bit PCM chunks at 22050 Hz, mono (matching ElevenLabs format).
        """
        logger.info(
            f"Synthesizing via Edge TTS: voice={self._voice} chars={len(text)}"
        )

        communicate = edge_tts.Communicate(
            text=text,
            voice=self._voice,
            rate="+0%",
            volume="+0%",
        )

        total_bytes = 0
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                data = chunk["data"]
                total_bytes += len(data)
                yield data

        logger.info(f"Edge TTS synthesis complete: {total_bytes:,} bytes")

    async def close(self) -> None:
        """No persistent resources to clean up."""
        pass
