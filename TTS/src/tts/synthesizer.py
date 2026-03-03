"""ElevenLabs TTS synthesizer via httpx streaming."""
import logging
from typing import AsyncGenerator

import httpx

from .config import config
from .exceptions import (
    ElevenLabsAuthError,
    ElevenLabsError,
    ElevenLabsNetworkError,
    ElevenLabsRateLimitError,
    ElevenLabsServerError,
)

logger = logging.getLogger(__name__)


def _raise_for_status(response: httpx.Response) -> None:
    """Raise typed ElevenLabs exceptions based on HTTP status code."""
    if response.status_code == 401:
        raise ElevenLabsAuthError(
            "Invalid or missing ElevenLabs API key", status_code=401
        )
    if response.status_code == 429:
        raise ElevenLabsRateLimitError(
            "ElevenLabs rate limit or quota exceeded", status_code=429
        )
    if response.status_code >= 500:
        raise ElevenLabsServerError(
            f"ElevenLabs server error: {response.status_code}",
            status_code=response.status_code,
        )
    if response.status_code >= 400:
        raise ElevenLabsError(
            f"ElevenLabs API error: {response.status_code}",
            status_code=response.status_code,
        )


class ElevenLabsSynthesizer:
    """Streams PCM audio from ElevenLabs API using httpx."""

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Synthesize text to PCM audio chunks via ElevenLabs streaming API.

        Yields raw 16-bit PCM chunks at 22050 Hz (pcm_22050 format).
        Raises ElevenLabsError subclasses on API failures.
        Raises ElevenLabsNetworkError on connection/timeout failures.
        """
        # output_format is a query parameter, not a body field
        url = (
            f"{config.elevenlabs_base_url}"
            f"/v1/text-to-speech/{config.elevenlabs_voice_id}/stream"
            f"?output_format={config.elevenlabs_output_format}"
        )
        headers = {
            "xi-api-key": config.elevenlabs_api_key,
            "Content-Type": "application/json",
        }
        body = {
            "text": text,
            "model_id": config.elevenlabs_model_id,
        }

        logger.info(
            f"Synthesizing via ElevenLabs: voice={config.elevenlabs_voice_id} "
            f"model={config.elevenlabs_model_id} chars={len(text)}"
        )

        try:
            async with httpx.AsyncClient(
                timeout=config.elevenlabs_connect_timeout
            ) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=body
                ) as response:
                    _raise_for_status(response)
                    chunk_count = 0
                    async for chunk in response.aiter_bytes(
                        chunk_size=config.chunk_size
                    ):
                        yield chunk
                        chunk_count += 1
                    logger.info(f"ElevenLabs synthesis complete: {chunk_count} chunks")

        except (ElevenLabsAuthError, ElevenLabsRateLimitError, ElevenLabsServerError, ElevenLabsError):
            raise
        except httpx.TimeoutException as exc:
            raise ElevenLabsNetworkError(f"ElevenLabs connection timeout: {exc}") from exc
        except httpx.NetworkError as exc:
            raise ElevenLabsNetworkError(f"ElevenLabs network error: {exc}") from exc

    async def close(self) -> None:
        """No persistent resources to clean up."""
        pass
