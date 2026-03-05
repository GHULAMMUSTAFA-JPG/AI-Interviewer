"""

Configuration management and MongoDB connection setup.
Includes performance optimization settings.
"""
import os
import logging
from logging.handlers import RotatingFileHandler
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv, find_dotenv
from cachetools import TTLCache

# Load environment variables — search up the directory tree so local dev
# picks up the root .env even when running from inside Main-Agent/
load_dotenv(find_dotenv(usecwd=True))

# Logging configuration
# LOG_FILE controls where logs go:
#   - Set to a path (default "agent.log") → stdout + rotating file (local dev)
#   - Set to "" or unset in Docker       → stdout only (Docker daemon captures it)
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
_log_file = os.getenv("LOG_FILE", "agent.log")
if _log_file:
    _log_handlers.append(
        RotatingFileHandler(
            _log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5               # keep agent.log + 5 rotated files
        )
    )

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-4s  %(message)s',
    datefmt='%H:%M:%S',
    handlers=_log_handlers
)

logger = logging.getLogger(__name__)

# ============================================================
# PERFORMANCE OPTIMIZATION SETTINGS
# ============================================================

# Cache Configuration
CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "1000"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "7200"))  # 2 hours

# Global interview context cache
interview_cache: TTLCache = TTLCache(
    maxsize=CACHE_MAX_SIZE,
    ttl=CACHE_TTL_SECONDS
)

# Performance Targets
TARGET_AVG_LATENCY_MS = 320
TARGET_P95_LATENCY_MS = 500
TARGET_P99_LATENCY_MS = 600

TARGET_PROMPT_TOKENS_MIN = 1200
TARGET_PROMPT_TOKENS_MAX = 1500
TARGET_CACHE_HIT_RATE = 0.99  # 99%

# LLM Configuration — all values controlled via .env
GEMINI_MODEL_CONVERSATION = os.getenv("GEMINI_MODEL_CONVERSATION", "gemini-2.0-flash")
GEMINI_MODEL_EVALUATION = os.getenv("GEMINI_MODEL_EVALUATION", "gemini-1.5-pro")
GEMINI_MAX_OUTPUT_TOKENS          = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "200"))
GEMINI_MAX_OUTPUT_TOKENS_COMBINED = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS_COMBINED", "400"))
GEMINI_TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.7"))
GEMINI_TOP_P = float(os.getenv("GEMINI_TOP_P", "0.9"))

# Retry Configuration (Tenacity)
RETRY_MAX_ATTEMPTS = 3
RETRY_WAIT_MULTIPLIER = 1
RETRY_WAIT_MIN = 1
RETRY_WAIT_MAX = 10

# Prompt Optimization
MAX_CONVERSATION_HISTORY = 3  # Last 3 messages only

# Fallback responses — set True to use hardcoded strings when LLM fails or validation fails.
# When False, exceptions propagate and validation warnings are logged but do not replace LLM output.
ENABLE_FALLBACK_RESPONSES = False

PHASE_INSTRUCTIONS = {
    "INTRO": (
        "Welcome the candidate and get them talking naturally. "
        "In the first turn, ask them to introduce themselves. "
        "Once they do, follow up on the most interesting thing they mentioned — a project, a technology, a gap, an achievement. "
        "Do not keep asking 'what drew you to this role' if the candidate has moved on; pick up whatever thread they opened."
    ),
    "EXPERIENCE": (
        "Dig into real work. When a candidate names a project or role, ask: what was your specific contribution, "
        "what was the hardest part, what was the outcome, and what would you do differently. "
        "Push for concrete numbers — team size, timeline, scale, impact. "
        "If their answer is vague, ask for a specific example."
    ),
    "TECHNICAL": (
        "Probe technical depth. When they mention a technology or system, ask why they chose it over alternatives, "
        "what trade-offs they faced, and how they debugged or scaled it. "
        "Ask follow-ups that expose whether they built it or just used it: "
        "'What was the hardest bug you hit?' or 'How would you redesign that now?'"
    ),
    "BEHAVIORAL": (
        "Use the STAR method naturally. Ask about a real situation: "
        "'Tell me about a time when...' then follow up on the specific actions they took and the outcome. "
        "Good topics: a project that failed, a disagreement with a teammate, delivering under tight deadlines, "
        "learning something difficult quickly."
    ),
    "CLOSING": (
        "Wrap up warmly. Thank the candidate for their time and specific answers. "
        "Invite them to ask anything about the role, the team, the tech stack, or the company culture. "
        "Answer their questions naturally as the hiring manager."
    ),
}

# ============================================================


class Config:
    """Application configuration"""

    # MongoDB
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    MONGODB_DB = os.getenv("MONGODB_DB", "interviews")

    # LLM Provider
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    # OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4-turbo")

    # Anthropic
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4")

    # TTS Service
    TTS_SERVICE_URL = os.getenv("TTS_SERVICE_URL", "http://localhost:8080/synthesize")

    # Safeguards
    SOFT_DURATION_LIMIT_MINUTES = int(os.getenv("SOFT_DURATION_LIMIT_MINUTES", "90"))
    HARD_DURATION_LIMIT_MINUTES = int(os.getenv("HARD_DURATION_LIMIT_MINUTES", "0"))  # 0 = disabled
    CONTEXT_WARNING_TOKENS = int(os.getenv("CONTEXT_WARNING_TOKENS", "150000"))
    CONTEXT_MAX_TOKENS = int(os.getenv("CONTEXT_MAX_TOKENS", "180000"))
    SILENCE_TIMEOUT_SECONDS = int(os.getenv("SILENCE_TIMEOUT_SECONDS", "180"))

    # Performance
    MAX_CONCURRENT_INTERVIEWS = int(os.getenv("MAX_CONCURRENT_INTERVIEWS", "50"))
    LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "10"))

    @classmethod
    def validate(cls):
        """Validate required configuration"""
        errors = []

        if not cls.MONGODB_URI:
            errors.append("MONGODB_URI is required")

        if cls.LLM_PROVIDER == "gemini" and not cls.GEMINI_API_KEY:
            errors.append("GEMINI_API_KEY is required when using Gemini provider")

        if cls.LLM_PROVIDER == "openai" and not cls.OPENAI_API_KEY:
            errors.append("OPENAI_API_KEY is required when using OpenAI provider")

        if cls.LLM_PROVIDER == "anthropic" and not cls.ANTHROPIC_API_KEY:
            errors.append("ANTHROPIC_API_KEY is required when using Anthropic provider")

        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")

        logger.info(f"Configuration validated - using {cls.LLM_PROVIDER} provider")


class MongoDB:
    """MongoDB connection manager"""

    _client: AsyncIOMotorClient | None = None
    _db = None

    @classmethod
    async def connect(cls):
        """Connect to MongoDB"""
        if cls._client is None:
            cls._client = AsyncIOMotorClient(Config.MONGODB_URI)
            cls._db = cls._client[Config.MONGODB_DB]
            logger.info(f"Connected to MongoDB: {Config.MONGODB_DB}")

    @classmethod
    async def disconnect(cls):
        """Disconnect from MongoDB"""
        if cls._client:
            cls._client.close()
            cls._client = None
            cls._db = None
            logger.info("Disconnected from MongoDB")

    @classmethod
    def get_db(cls):
        """Get database instance"""
        if cls._db is None:
            raise RuntimeError("MongoDB not connected. Call MongoDB.connect() first")
        return cls._db

    @classmethod
    def get_collection(cls, name: str):
        """Get collection by name"""
        return cls.get_db()[name]
