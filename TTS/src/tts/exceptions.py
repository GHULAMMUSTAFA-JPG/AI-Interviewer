"""Custom exceptions for ElevenLabs TTS integration."""


class ElevenLabsError(Exception):
    """Base exception for all ElevenLabs API errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ElevenLabsAuthError(ElevenLabsError):
    """Raised on HTTP 401 — invalid or missing API key."""


class ElevenLabsRateLimitError(ElevenLabsError):
    """Raised on HTTP 429 — quota or rate limit exceeded."""


class ElevenLabsServerError(ElevenLabsError):
    """Raised on HTTP 5xx — ElevenLabs server-side error."""


class ElevenLabsNetworkError(ElevenLabsError):
    """Raised on connection failures or timeouts."""
