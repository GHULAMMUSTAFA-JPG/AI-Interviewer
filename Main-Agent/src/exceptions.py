"""
Custom exception hierarchy for the AI Interview Agent.
"""


class AgentException(Exception):
    """Base exception for all agent errors"""
    pass


# Database Exceptions
class DatabaseException(AgentException):
    """Base exception for database errors"""
    pass


class DatabaseConnectionError(DatabaseException):
    """Database connection failed"""
    pass


class DatabaseTimeoutError(DatabaseException):
    """Database query timed out"""
    pass


class DocumentNotFoundError(DatabaseException):
    """Required document not found in database"""
    pass


# LLM Exceptions
class LLMException(AgentException):
    """Base exception for LLM errors"""
    pass


class LLMTimeoutError(LLMException):
    """LLM request timed out"""
    pass


class LLMRateLimitError(LLMException):
    """LLM rate limit exceeded"""
    pass


class LLMInvalidResponseError(LLMException):
    """LLM returned invalid response"""
    pass


class LLMServiceUnavailableError(LLMException):
    """LLM service is unavailable"""
    pass


# Validation Exceptions
class ValidationException(AgentException):
    """Base exception for validation errors"""
    pass


class OutputValidationError(ValidationException):
    """LLM output failed validation"""
    pass


class InputValidationError(ValidationException):
    """Input data failed validation"""
    pass


# TTS Exceptions
class TTSException(AgentException):
    """Base exception for TTS errors"""
    pass


class TTSServiceDownError(TTSException):
    """TTS service is down"""
    pass


class TTSGenerationError(TTSException):
    """TTS audio generation failed"""
    pass


# Business Logic Exceptions
class BusinessException(AgentException):
    """Base exception for business logic errors"""
    pass


class InterviewCompletedError(BusinessException):
    """Interview is already completed"""
    pass


class InvalidPhaseError(BusinessException):
    """Invalid interview phase"""
    pass


class SafeguardTriggeredError(BusinessException):
    """Interview safeguard was triggered"""
    pass
