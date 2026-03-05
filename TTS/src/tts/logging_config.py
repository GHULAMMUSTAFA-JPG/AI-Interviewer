"""Human-readable logging for TTS service."""
import logging
import sys


class PlainFormatter(logging.Formatter):
    """HH:MM:SS  LEVEL  [module]  message"""

    def format(self, record: logging.LogRecord) -> str:
        ts    = self.formatTime(record, "%H:%M:%S")
        level = record.levelname[:4]
        # Use only the last component of the dotted logger name
        name  = record.name.split(".")[-1]
        msg   = record.getMessage()
        line  = f"{ts}  {level:<4}  [{name}]  {msg}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with plain-text formatter."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(PlainFormatter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)
