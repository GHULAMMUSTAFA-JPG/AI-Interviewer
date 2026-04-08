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


def _raise_for_status(response: httpx.Response, api_key_index: int, api_key: str, body: str = "") -> None:
    """Raise typed ElevenLabs exceptions based on HTTP status code."""
    detail = f" — {body[:300]}" if body else ""
    if response.status_code == 401:
        raise ElevenLabsAuthError(
            f"ElevenLabs auth failed (key #{api_key_index + 1}, key={api_key[:4]}...{api_key[-4:]}){detail}",
            status_code=401,
        )
    if response.status_code == 402:
        # Payment required - try next key
        raise ElevenLabsAuthError(
            f"ElevenLabs API key #{api_key_index + 1} ({api_key[:4]}...{api_key[-4:]}) has no credits or quota (HTTP 402){detail} - trying next key",
            status_code=402,
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
        self._key_health: dict[int, dict] = {}  # Track per-key success/failure
        logger.info(f"ElevenLabsSynthesizer initialized with {len(self._api_keys)} API keys")

    async def validate_key(self, api_key: str, key_index: int) -> dict | None:
        """
        Validate an ElevenLabs API key by testing the actual TTS endpoint.
        We use a minimal text synthesis request rather than /v1/user because
        most API keys lack the 'user_read' permission required by /v1/user.
        Returns dict with validation result.
        """
        model_id = config.elevenlabs_model_id
        voice_id = config.elevenlabs_voice_id
        output_format = config.elevenlabs_output_format

        url = (
            f"{config.elevenlabs_base_url}"
            f"/v1/text-to-speech/{voice_id}/stream"
            f"?output_format={output_format}"
        )
        headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
        body = {"text": "Test.", "model_id": model_id}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                async with client.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code == 200:
                        total_bytes = 0
                        async for chunk in resp.aiter_bytes():
                            total_bytes += len(chunk)
                        return {
                            "valid": True,
                            "bytes_generated": total_bytes,
                            "model": model_id,
                            "voice": voice_id,
                        }
                    elif resp.status_code == 401:
                        detail = ""
                        try:
                            detail = resp.json().get("detail", {}).get("message", resp.text[:200])
                        except Exception:
                            detail = resp.text[:200]
                        logger.error(f"Key #{key_index + 1} INVALID (401): {detail}")
                        return {"valid": False, "error": "invalid_key", "detail": detail}
                    elif resp.status_code == 402:
                        detail = ""
                        try:
                            detail = resp.json().get("detail", {}).get("message", resp.text[:200])
                        except Exception:
                            detail = resp.text[:200]
                        logger.error(f"Key #{key_index + 1} NO QUOTA (402): {detail}")
                        return {"valid": False, "error": "no_quota", "detail": detail}
                    elif resp.status_code == 404:
                        detail = ""
                        try:
                            detail = resp.json().get("detail", {}).get("message", resp.text[:200])
                        except Exception:
                            detail = resp.text[:200]
                        logger.error(
                            f"Key #{key_index + 1} NOT FOUND (404): {detail} "
                            f"— voice={voice_id} or model={model_id} may be invalid"
                        )
                        return {"valid": False, "error": "not_found", "detail": detail}
                    else:
                        await resp.aread()
                        logger.error(f"Key #{key_index + 1} HTTP {resp.status_code}: {resp.text[:200]}")
                        return {"valid": False, "error": f"http_{resp.status_code}", "detail": resp.text[:200]}
        except Exception as e:
            logger.error(f"Key #{key_index + 1} validation error: {e}")
            return {"valid": False, "error": "exception", "detail": str(e)}

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Synthesize text to PCM audio chunks via ElevenLabs streaming API.

        Tries API keys in order, prioritizing keys with recent success.
        Skips keys known to have failed recently.

        Yields raw 16-bit PCM chunks at 22050 Hz (pcm_22050 format).
        Raises ElevenLabsError subclasses on API failures.
        Raises ElevenLabsNetworkError on connection/timeout failures.
        """
        # Sort keys by health: successful keys first, unknown next, failed last
        ordered_keys = sorted(
            enumerate(self._api_keys),
            key=lambda x: self._key_health.get(x[0], {}).get("score", 0),
            reverse=True
        )

        # Try each API key in priority order
        for key_index, api_key in ordered_keys:
            # Skip keys known to have quota issues
            key_health = self._key_health.get(key_index, {})
            if key_health.get("blocked") in ("no_quota", "invalid_key"):
                logger.warning(f"Skipping key #{key_index + 1} (known {key_health['blocked']})")
                continue

            try:
                async for chunk in self._synthesize_with_key(text, api_key, key_index):
                    yield chunk
                # Success — update key health
                self._current_key_index = key_index
                self._key_health[key_index] = {
                    "score": self._key_health.get(key_index, {}).get("score", 0) + 1,
                    "last_success": "now",
                    "blocked": None,
                }
                return
            except ElevenLabsAuthError as e:
                if "402" in str(e) or "no credits" in str(e).lower() or "quota" in str(e).lower():
                    # Mark this key as blocked — don't try it again
                    self._key_health[key_index] = {"score": -100, "blocked": "no_quota"}
                else:
                    self._key_health[key_index] = {"score": -100, "blocked": "invalid_key"}
                logger.warning(f"API key #{key_index + 1} blocked: {e}")
                continue
            except (ElevenLabsServerError) as e:
                logger.warning(f"API key #{key_index + 1} server error: {e}")
                self._key_health[key_index] = {
                    "score": self._key_health.get(key_index, {}).get("score", 0) - 1,
                    "last_error": str(e)[:100],
                }
                if key_index < len(self._api_keys) - 1:
                    continue
                else:
                    logger.error(f"All {len(self._api_keys)} API keys failed")
                    raise
            except (ElevenLabsRateLimitError, ElevenLabsNetworkError) as e:
                logger.error(f"Rate limit or network error (key #{key_index + 1}): {e}")
                raise
            except Exception as e:
                logger.error(f"Unexpected error with API key #{key_index + 1}: {e}")
                continue

        # All keys exhausted
        raise ElevenLabsError("All ElevenLabs API keys failed or are blocked")

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
                    if response.status_code >= 400:
                        await response.aread()
                        _raise_for_status(response, key_index, api_key, response.text)
                    else:
                        _raise_for_status(response, key_index, api_key)
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
