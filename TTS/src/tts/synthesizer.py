"""ElevenLabs TTS synthesizer via httpx streaming."""
import logging
from typing import AsyncGenerator, List, Tuple

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


def _raise_for_status(response: httpx.Response, api_key_index: int) -> None:
    """Raise typed ElevenLabs exceptions based on HTTP status code."""
    if response.status_code == 401:
        raise ElevenLabsAuthError(
            f"Invalid or missing ElevenLabs API key (key #{api_key_index + 1})", status_code=401
        )
    if response.status_code == 402:
        # Payment required - try next key
        raise ElevenLabsAuthError(
            f"ElevenLabs API key #{api_key_index + 1} ({api_key[:4]}...{api_key[-4:]}) has no credits or quota (HTTP 402) - trying next key", 
            status_code=402
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
    """Streams PCM audio from ElevenLabs API using httpx with API key fallback."""

    def __init__(self):
        self._api_keys = config.elevenlabs_api_keys
        self._current_key_index = 0
        logger.info(f"ElevenLabsSynthesizer initialized with {len(self._api_keys)} API keys")

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Synthesize text to PCM audio chunks via ElevenLabs streaming API.
        
        Tries API keys in order until one succeeds or all fail.
        
        Yields raw 16-bit PCM chunks at 22050 Hz (pcm_22050 format).
        Raises ElevenLabsError subclasses on API failures.
        Raises ElevenLabsNetworkError on connection/timeout failures.
        """
        # Try each API key in order
        for key_index, api_key in enumerate(self._api_keys):
            try:
                async for chunk in self._synthesize_with_key(text, api_key, key_index):
                    yield chunk
                # Success - update current key index for next time
                self._current_key_index = key_index
                return
            except (ElevenLabsAuthError, ElevenLabsServerError) as e:
                # Auth/server errors - try next key
                logger.warning(f"API key #{key_index + 1} failed: {e}")
                if key_index < len(self._api_keys) - 1:
                    logger.info(f"Trying next API key ({key_index + 2}/{len(self._api_keys)})...")
                    continue
                else:
                    # No more keys to try
                    logger.error(f"All {len(self._api_keys)} API keys failed")
                    raise
            except (ElevenLabsRateLimitError, ElevenLabsNetworkError) as e:
                # Rate limit/network errors - don't try next key, just fail
                logger.error(f"Rate limit or network error: {e}")
                raise
            except Exception as e:
                logger.error(f"Unexpected error with API key #{key_index + 1}: {e}")
                if key_index < len(self._api_keys) - 1:
                    continue
                raise

        # Should never reach here, but just in case
        raise ElevenLabsError("All ElevenLabs API keys failed")

    async def _synthesize_with_key(
        self, text: str, api_key: str, key_index: int
    ) -> AsyncGenerator[bytes, None]:
        """Synthesize using a specific API key."""
        url = (
            f"{config.elevenlabs_base_url}"
            f"/v1/text-to-speech/{config.elevenlabs_voice_id}/stream"
            f"?output_format={config.elevenlabs_output_format}"
        )
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
        }
        body = {
            "text": text,
            "model_id": config.elevenlabs_model_id,
        }

        logger.info(
            f"Synthesizing via ElevenLabs (key #{key_index + 1}): voice={config.elevenlabs_voice_id} "
            f"model={config.elevenlabs_model_id} chars={len(text)}"
        )

        try:
            async with httpx.AsyncClient(
                timeout=config.elevenlabs_connect_timeout
            ) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=body
                ) as response:
                    _raise_for_status(response, key_index)
                    chunk_count = 0
                    async for chunk in response.aiter_bytes(
                        chunk_size=config.chunk_size
                    ):
                        yield chunk
                        chunk_count += 1
                    logger.info(f"ElevenLabs synthesis complete (key #{key_index + 1}): {chunk_count} chunks")

        except (ElevenLabsAuthError, ElevenLabsRateLimitError, ElevenLabsServerError, ElevenLabsError):
            raise
        except httpx.TimeoutException as exc:
            raise ElevenLabsNetworkError(f"ElevenLabs connection timeout: {exc}") from exc
        except httpx.NetworkError as exc:
            raise ElevenLabsNetworkError(f"ElevenLabs network error: {exc}") from exc

    async def close(self) -> None:
        """No persistent resources to clean up."""
        pass
