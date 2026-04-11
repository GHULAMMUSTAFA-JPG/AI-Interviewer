"""Custom exceptions for TTS service."""


class TTSError(Exception):
    """Base exception for TTS errors."""


class TTSSynthesisError(TTSError):
    """Raised when speech synthesis fails."""


class TTSNetworkError(TTSError):
    """Raised on network/connectivity failures during synthesis."""
