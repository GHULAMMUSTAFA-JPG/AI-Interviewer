"""ElevenLabs streaming TTS synthesizer.

Uses eleven_flash_v2_5 (~75-150ms TTFB) and streams raw PCM s16le at 22050 Hz
directly to AudioPlayer. No mpg123 decoding needed — ElevenLabs returns PCM
natively via output_format=pcm_22050.

Latency comparison vs Edge TTS:
  Edge TTS:    buffer all MP3 → mpg123 decode → yield PCM  = 400-700ms before first audio
  ElevenLabs:  PCM streams from first packet               = 75-150ms TTFB
"""
import asyncio
import logging
from typing import AsyncGenerator

import httpx

from .config import config

logger = logging.getLogger(__name__)

_API_BASE = "https://api.elevenlabs.io/v1"
_MODEL = "eleven_flash_v2_5"   # ~75-100ms TTFB — fastest model
_OUTPUT_FORMAT = "pcm_22050"   # raw s16le at 22050 Hz, no decode step
_CHUNK_SIZE = 4096
_MAX_RETRIES = 3
_RETRY_BASE_DELAY_SEC = 1.0


class ElevenLabsSynthesizer:
    """Streams raw PCM from ElevenLabs — minimum TTFB, zero decode overhead."""

    def __init__(self) -> None:
        self._api_key = config.elevenlabs_api_key
        self._voice_id = config.elevenlabs_voice_id
        logger.info(
            f"ElevenLabsSynthesizer initialized: voice={self._voice_id} model={_MODEL}"
        )

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """Stream PCM s16le at 22050 Hz. First bytes arrive in ~75-150ms."""
        logger.info(f"Synthesizing via ElevenLabs Flash: chars={len(text)}")

        last_exc: Exception = RuntimeError("ElevenLabs: no attempts made")

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
                    async with client.stream(
                        "POST",
                        f"{_API_BASE}/text-to-speech/{self._voice_id}/stream",
                        headers={
                            "xi-api-key": self._api_key,
                            "Content-Type": "application/json",
                        },
                        params={"output_format": _OUTPUT_FORMAT},
                        json={
                            "text": text,
                            "model_id": _MODEL,
                            "voice_settings": {
                                "stability": 0.5,
                                "similarity_boost": 0.75,
                                "speed": 1.0,
                            },
                        },
                    ) as response:
                        if response.status_code != 200:
                            body = await response.aread()
                            err = body.decode(errors="replace")[:200]
                            exc = RuntimeError(f"ElevenLabs HTTP {response.status_code}: {err}")
                            # 4xx errors are not retryable
                            if 400 <= response.status_code < 500:
                                raise exc
                            raise exc

                        received = 0
                        async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                            if chunk:
                                received += len(chunk)
                                yield chunk

                        logger.info(
                            f"ElevenLabs synthesis complete "
                            f"(attempt {attempt}): {received:,} bytes PCM"
                        )
                        return  # success

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
            except RuntimeError as exc:
                last_exc = exc
                if "HTTP 4" in str(exc):
                    raise
            except Exception as exc:
                last_exc = exc

            if attempt < _MAX_RETRIES:
                delay = _RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                logger.warning(
                    f"ElevenLabs attempt {attempt}/{_MAX_RETRIES} failed: {last_exc} "
                    f"— retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"ElevenLabs failed after {_MAX_RETRIES} attempts: {last_exc}")

        raise last_exc

    async def close(self) -> None:
        """No persistent resources."""
        pass
