"""Edge TTS synthesizer — free, no API key required.

Uses Microsoft's Edge browser's built-in TTS service via the edge-tts library.
Edge TTS returns MP3 audio; mpg123 decodes it to raw s16le PCM before handing
off to AudioPlayer (pacat), which expects uncompressed PCM samples.

Voice list: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts#edge-tts
Recommended voices:
  - en-US-GuyNeural (male, natural)
  - en-US-JennyNeural (female, natural)
  - en-GB-RyanNeural (British male)
  - en-GB-SoniaNeural (British female)
"""
import asyncio
import logging
from typing import AsyncGenerator

import edge_tts

from .config import config

logger = logging.getLogger(__name__)


class EdgeTTSSynthesizer:
    """Generates PCM audio via Microsoft Edge TTS (free, no API key).

    Pipeline: edge_tts → MP3 bytes → mpg123 → s16le PCM at 22050 Hz mono
    """

    def __init__(self):
        self._voice = config.edge_tts_voice
        logger.info(f"EdgeTTSSynthesizer initialized with voice={self._voice}")

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Synthesize text and yield raw 16-bit PCM chunks at 22050 Hz mono.

        Edge TTS returns MP3; mpg123 converts it to PCM on the fly.
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

        # Collect MP3 data from Edge TTS
        mp3_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_chunks.append(chunk["data"])

        if not mp3_chunks:
            logger.warning("Edge TTS returned no audio data")
            return

        mp3_data = b"".join(mp3_chunks)
        logger.info(f"Edge TTS MP3 received: {len(mp3_data):,} bytes — decoding to PCM via mpg123")

        # Decode MP3 → raw s16le PCM at 22050 Hz mono using mpg123
        proc = await asyncio.create_subprocess_exec(
            "mpg123",
            "-q",           # quiet — suppress progress output
            "-r", "22050",  # resample to 22050 Hz
            "-m",           # mono
            "-s",           # raw signed 16-bit samples to stdout
            "-",            # read from stdin
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        pcm_data, stderr = await proc.communicate(input=mp3_data)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:300]
            raise RuntimeError(f"mpg123 decode failed (rc={proc.returncode}): {err}")

        logger.info(f"Edge TTS synthesis complete: {len(mp3_data):,} bytes MP3 → {len(pcm_data):,} bytes PCM")

        # Yield PCM in 4 KB chunks (same as ElevenLabs path)
        chunk_size = 4096
        for i in range(0, len(pcm_data), chunk_size):
            yield pcm_data[i : i + chunk_size]

    async def close(self) -> None:
        """No persistent resources to clean up."""
        pass
