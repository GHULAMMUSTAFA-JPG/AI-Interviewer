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

This is the only TTS synthesizer used in the edge-tts branch.
"""
import asyncio
import logging
from typing import AsyncGenerator

import edge_tts

from .config import config

logger = logging.getLogger(__name__)

# Retry settings for transient Edge TTS network failures
_MAX_RETRIES = 3
_RETRY_BASE_DELAY_SEC = 2.0


class EdgeTTSSynthesizer:
    """Generates PCM audio via Microsoft Edge TTS (free, no API key).

    Pipeline: edge_tts → MP3 bytes → mpg123 → s16le PCM at 22050 Hz mono

    Retries up to 3 times on network / service errors with exponential backoff
    so a single transient failure does not silence the bot for a full turn.
    """

    def __init__(self):
        self._voice = config.edge_tts_voice
        logger.info(f"EdgeTTSSynthesizer initialized with voice={self._voice}")

    async def _fetch_mp3(self, text: str) -> bytes:
        """
        Download MP3 from Edge TTS with retry on transient failures.
        Returns raw MP3 bytes or raises on permanent failure.
        """
        last_exc: Exception = RuntimeError("Edge TTS: no attempts made")
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                communicate = edge_tts.Communicate(
                    text=text,
                    voice=self._voice,
                    rate="+0%",
                    volume="+0%",
                )
                mp3_chunks = []
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        mp3_chunks.append(chunk["data"])

                if not mp3_chunks:
                    raise RuntimeError("Edge TTS returned no audio data")

                mp3_data = b"".join(mp3_chunks)
                logger.info(
                    f"Edge TTS MP3 received (attempt {attempt}): {len(mp3_data):,} bytes"
                )
                return mp3_data

            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                    logger.warning(
                        f"Edge TTS attempt {attempt}/{_MAX_RETRIES} failed: {exc} "
                        f"— retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        f"Edge TTS failed after {_MAX_RETRIES} attempts: {exc}"
                    )
        raise last_exc

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Synthesize text and yield raw 16-bit PCM chunks at 22050 Hz mono.

        Retries the Edge TTS fetch up to 3 times before raising.
        mpg123 is used to decode MP3 → PCM synchronously after all data arrives.
        """
        logger.info(
            f"Synthesizing via Edge TTS: voice={self._voice} chars={len(text)}"
        )

        # Step 1: Fetch MP3 (with retry)
        mp3_data = await self._fetch_mp3(text)

        # Step 2: Decode MP3 → raw s16le PCM at 22050 Hz mono using mpg123
        logger.info(f"Decoding {len(mp3_data):,} bytes MP3 → PCM via mpg123")
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

        logger.info(
            f"Edge TTS synthesis complete: "
            f"{len(mp3_data):,} bytes MP3 → {len(pcm_data):,} bytes PCM"
        )

        # Step 3: Yield PCM in 4 KB chunks
        chunk_size = config.chunk_size
        for i in range(0, len(pcm_data), chunk_size):
            yield pcm_data[i : i + chunk_size]

    async def close(self) -> None:
        """No persistent resources to clean up."""
        pass
