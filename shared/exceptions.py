"""
Shared Exceptions for AI Interviewer Services

Common exception classes used across all services.
"""


class DatabaseException(Exception):
    """Raised when a database operation fails."""
    pass


class DocumentNotFoundError(DatabaseException):
    """Raised when a requested document is not found."""
    pass


class RedisException(Exception):
    """Raised when a Redis operation fails."""
    pass
