"""
Optimized multi-model LLM provider abstraction.

Supports: Gemini Flash 2.5, OpenAI GPT-4, Anthropic Claude

Performance optimizations:
- Uses tenacity for cleaner retry logic
- Structured logging with structlog
- Performance metrics tracking
"""
from abc import ABC, abstractmethod
from google import genai
from google.genai import types
import re
import time
import structlog
from tenacity import (
    retry,
    stop_after_attempt,
    retry_if_exception_type,
    before_sleep_log
)
import logging

from src.config import (
    Config,
    logger,
    GEMINI_MODEL_CONVERSATION,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_TEMPERATURE,
    GEMINI_TOP_P,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MIN,
    RETRY_WAIT_MAX
)
from src.exceptions import LLMException
from src.agent.retry import llm_circuit_breaker, summary_circuit_breaker

logger_struct = structlog.get_logger()


def _parse_retry_delay(exc: Exception) -> float:
    """
    Extract the server-recommended retry delay from a 429 error.

    Gemini returns: {'@type': '...RetryInfo', 'retryDelay': '41s'}
    Falls back to RETRY_WAIT_MAX if the field is absent or unparseable.
    """
    match = re.search(r"retryDelay['\"]:\s*['\"](\d+(?:\.\d+)?)s", str(exc))
    if match:
        return float(match.group(1))
    return float(RETRY_WAIT_MAX)


def _gemini_wait(retry_state) -> float:
    """
    Custom tenacity wait: honour the server's retryDelay on 429,
    use exponential backoff for every other error.
    """
    exc = retry_state.outcome.exception()
    if exc and ("429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)):
        delay = _parse_retry_delay(exc)
        logger_struct.warning(
            "rate_limited_honouring_retry_delay",
            delay_seconds=delay,
            attempt=retry_state.attempt_number
        )
        return delay
    # Exponential backoff for non-rate-limit errors
    return min(RETRY_WAIT_MIN * (2 ** (retry_state.attempt_number - 1)), RETRY_WAIT_MAX)


class LLMProvider(ABC):
    """Abstract base class for LLM providers"""

    @abstractmethod
    async def generate(self, prompt: str) -> str:
        """Generate response from LLM"""
        pass

    async def generate_summary(self, prompt: str) -> str:
        """Generate a summary response.  Default: delegates to generate().
        Override in providers that need a separate circuit breaker for summaries.
        """
        return await self.generate(prompt)


class GeminiProvider(LLMProvider):
    """Optimized Gemini Flash 2.5 implementation with tenacity retry"""

    def __init__(self):
        self.client = genai.Client(api_key=Config.GEMINI_API_KEY)
        self.model = GEMINI_MODEL_CONVERSATION
        logger_struct.info("gemini_provider_initialized", model=self.model)

    async def _call_gemini(self, prompt: str) -> str:
        """Core Gemini API call — no retry, no circuit breaker."""
        start_time = time.perf_counter()
        try:
            logger_struct.debug("gemini_request_starting", model=self.model)

            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
                    temperature=GEMINI_TEMPERATURE,
                    top_p=GEMINI_TOP_P
                )
            )

            if not response.text:
                raise LLMException("Gemini returned empty response")

            latency_ms = (time.perf_counter() - start_time) * 1000
            logger_struct.info(
                "gemini_response_generated",
                latency_ms=round(latency_ms, 2),
                response_length=len(response.text),
                model=self.model
            )
            return response.text

        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger_struct.error(
                "gemini_generation_failed",
                error=str(e),
                error_type=type(e).__name__,
                latency_ms=round(latency_ms, 2),
                exc_info=True
            )
            raise LLMException(f"Gemini error: {e}")

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=_gemini_wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger_struct, logging.WARNING),
        reraise=True
    )
    async def generate(self, prompt: str) -> str:
        """Generate interview response through the main circuit breaker."""
        return await llm_circuit_breaker.call(self._call_gemini, prompt)

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=_gemini_wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger_struct, logging.WARNING),
        reraise=True
    )
    async def generate_summary(self, prompt: str) -> str:
        """Generate summary through a separate circuit breaker.
        Failures here cannot open the main interview circuit breaker.
        """
        return await summary_circuit_breaker.call(self._call_gemini, prompt)


class OpenAIProvider(LLMProvider):
    """OpenAI GPT-4 implementation (for future)"""

    def __init__(self):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=Config.OPENAI_API_KEY)
        self.model = Config.OPENAI_MODEL
        logger.info(f"Initialized OpenAI provider: {self.model}")

    async def generate(self, prompt: str) -> str:
        """Generate response using OpenAI GPT-4"""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.7
            )

            return response.choices[0].message.content

        except Exception as e:
            logger.error(f"OpenAI generation failed: {e}")
            raise LLMException(f"OpenAI error: {e}")


class AnthropicProvider(LLMProvider):
    """Anthropic Claude implementation with retry and circuit breaker."""

    def __init__(self):
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=Config.ANTHROPIC_API_KEY)
        self.model = Config.ANTHROPIC_MODEL
        logger_struct.info("anthropic_provider_initialized", model=self.model)

    async def _call_anthropic(self, prompt: str) -> str:
        """Core Anthropic API call — no retry, no circuit breaker."""
        start_time = time.perf_counter()
        try:
            response = await self.client.messages.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
                temperature=GEMINI_TEMPERATURE,
            )
            text = response.content[0].text
            if not text:
                raise LLMException("Anthropic returned empty response")
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger_struct.info(
                "anthropic_response_generated",
                latency_ms=round(latency_ms, 2),
                response_length=len(text),
                model=self.model,
            )
            return text
        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger_struct.error(
                "anthropic_generation_failed",
                error=str(e),
                error_type=type(e).__name__,
                latency_ms=round(latency_ms, 2),
                exc_info=True,
            )
            raise LLMException(f"Anthropic error: {e}")

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=_gemini_wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger_struct, logging.WARNING),
        reraise=True,
    )
    async def generate(self, prompt: str) -> str:
        return await llm_circuit_breaker.call(self._call_anthropic, prompt)

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=_gemini_wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger_struct, logging.WARNING),
        reraise=True,
    )
    async def generate_summary(self, prompt: str) -> str:
        return await summary_circuit_breaker.call(self._call_anthropic, prompt)


# Module-level singleton — created once at first call, reused for every message.
# This avoids creating a new genai.Client (HTTP session + TLS handshake) on every turn.
_provider_instance: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    """Return the singleton LLM provider, initializing it on first call."""
    global _provider_instance

    if _provider_instance is None:
        provider = Config.LLM_PROVIDER.lower()

        if provider == "gemini":
            _provider_instance = GeminiProvider()
        elif provider == "openai":
            _provider_instance = OpenAIProvider()
        elif provider == "anthropic":
            _provider_instance = AnthropicProvider()
        else:
            raise ValueError(f"Unknown LLM provider: {provider}")

        logger_struct.info("llm_provider_singleton_created", provider=provider)

    return _provider_instance
