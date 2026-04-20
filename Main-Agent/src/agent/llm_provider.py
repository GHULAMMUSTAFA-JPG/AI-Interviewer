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
import json
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
    GEMINI_MAX_OUTPUT_TOKENS_COMBINED,
    GEMINI_TEMPERATURE,
    GEMINI_TOP_P,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MIN,
    RETRY_WAIT_MAX
)
from src.exceptions import LLMException
from src.agent.retry import llm_circuit_breaker, summary_circuit_breaker
from src.metrics import llm_calls_total, llm_call_duration_seconds

logger_struct = structlog.get_logger()

# Session-scoped LLM call counter — reset on process restart, never persisted.
_llm_call_count: int = 0


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

    async def generate_combined(self, prompt: str) -> dict:
        """Generate interview response + summary in one call.
        Default: falls back to generate() only (no summary update).
        Override in providers that support JSON output mode.
        Returns: {"response": str, "summary": str | None}
        """
        response = await self.generate(prompt)
        return {"response": response, "summary": None}

    async def generate_json(self, prompt: str) -> dict:
        """Generate a structured JSON response.
        Default: call generate() and parse the text as JSON.
        GeminiProvider overrides this to use native JSON output mode.
        """
        text = await self.generate(prompt)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.MULTILINE)
            return json.loads(cleaned)


class GeminiProvider(LLMProvider):
    """Optimized Gemini Flash 2.5 implementation with tenacity retry"""

    def __init__(self):
        self.client = genai.Client(api_key=Config.GEMINI_API_KEY)
        self.model = GEMINI_MODEL_CONVERSATION
        logger_struct.info("gemini_provider_initialized", model=self.model)

    async def _call_gemini(self, prompt: str) -> str:
        """Core Gemini API call — no retry, no circuit breaker."""
        global _llm_call_count
        _llm_call_count += 1
        call_num = _llm_call_count

        start_time = time.perf_counter()
        logger_struct.info("llm_call", call_num=call_num, model=self.model)
        try:
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
                "llm_call_success",
                call_num=call_num,
                latency_ms=round(latency_ms, 2),
                response_length=len(response.text),
                model=self.model
            )
            llm_calls_total.labels(provider="gemini", status="success").inc()
            llm_call_duration_seconds.labels(provider="gemini").observe(latency_ms / 1000)
            return response.text

        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            is_rate_limit = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
            if is_rate_limit:
                logger_struct.error(
                    "RATE_LIMITED",
                    call_num=call_num,
                    model=self.model,
                    session_calls=call_num,
                    hint="quota exhausted — switch API key or wait for daily reset"
                )
                llm_calls_total.labels(provider="gemini", status="rate_limited").inc()
            else:
                logger_struct.error(
                    "llm_call_failed",
                    call_num=call_num,
                    error=str(e)[:120],
                    error_type=type(e).__name__,
                    latency_ms=round(latency_ms, 2),
                    exc_info=True
                )
                llm_calls_total.labels(provider="gemini", status="error").inc()
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

    async def _call_gemini_json(self, prompt: str) -> dict:
        """Core Gemini API call with JSON output mode — no retry, no circuit breaker."""
        start_time = time.perf_counter()
        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS_COMBINED,
                    temperature=GEMINI_TEMPERATURE,
                    top_p=GEMINI_TOP_P,
                    response_mime_type="application/json",
                )
            )

            if not response.text:
                raise LLMException("Gemini returned empty JSON response")

            latency_ms = (time.perf_counter() - start_time) * 1000
            logger_struct.info(
                "gemini_json_response_generated",
                latency_ms=round(latency_ms, 2),
                response_length=len(response.text),
            )

            try:
                return json.loads(response.text)
            except json.JSONDecodeError:
                cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', response.text.strip(), flags=re.MULTILINE)
                return json.loads(cleaned)

        except LLMException:
            raise
        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger_struct.error(
                "gemini_json_generation_failed",
                error=str(e),
                latency_ms=round(latency_ms, 2),
                exc_info=True
            )
            raise LLMException(f"Gemini JSON error: {e}")

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=_gemini_wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger_struct, logging.WARNING),
        reraise=True
    )
    async def generate_combined(self, prompt: str) -> dict:
        """ONE call returning {"response": str, "summary": str} using JSON output mode."""
        return await summary_circuit_breaker.call(self._call_gemini_json, prompt)

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=_gemini_wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger_struct, logging.WARNING),
        reraise=True
    )
    async def generate_json(self, prompt: str) -> dict:
        """Generate a structured JSON response using native Gemini JSON output mode."""
        return await summary_circuit_breaker.call(self._call_gemini_json, prompt)


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


class QwenProvider(LLMProvider):
    """Qwen via OpenRouter free tier — OpenAI-compatible API."""

    def __init__(self):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(
            api_key=Config.OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
        )
        self.model = Config.QWEN_MODEL
        logger_struct.info("qwen_provider_initialized", model=self.model)

    async def _call_qwen(self, prompt: str) -> str:
        """Core Qwen API call — no retry, no circuit breaker."""
        start_time = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                temperature=0.7,
            )
            text = response.choices[0].message.content
            if not text:
                raise LLMException("Qwen returned empty response")
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger_struct.info(
                "qwen_response_generated",
                latency_ms=round(latency_ms, 2),
                response_length=len(text),
                model=self.model,
            )
            return text
        except LLMException:
            raise
        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            logger_struct.error(
                "qwen_generation_failed",
                error=str(e),
                error_type=type(e).__name__,
                latency_ms=round(latency_ms, 2),
                exc_info=True,
            )
            raise LLMException(f"Qwen error: {e}")

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=_gemini_wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger_struct, logging.WARNING),
        reraise=True,
    )
    async def generate(self, prompt: str) -> str:
        return await llm_circuit_breaker.call(self._call_qwen, prompt)

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=_gemini_wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger_struct, logging.WARNING),
        reraise=True,
    )
    async def generate_combined(self, prompt: str) -> dict:
        """
        Qwen doesn't support JSON mode natively, so we call generate() and parse.
        Returns {"response": text, "summary": {}} — compatible with pipeline.
        """
        text = await self._call_qwen(prompt)
        metadata = {}
        # Try to extract JSON from the response
        # Use multiline dotall so nested JSON objects and preambles are handled correctly.
        # Old pattern r"\{[^}]*\}" would miss nested braces and silently fail.
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            try:
                parsed = json.loads(json_match.group())
                if "response" in parsed:
                    return parsed
                metadata = parsed
            except json.JSONDecodeError:
                pass
        return {"response": text, "summary": metadata}

    @retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=_gemini_wait,
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger_struct, logging.WARNING),
        reraise=True,
    )
    async def generate_summary(self, prompt: str) -> str:
        return await summary_circuit_breaker.call(self._call_qwen, prompt)


class DeepSeekProvider(LLMProvider):
    """DeepSeek via their OpenAI-compatible API."""

    def __init__(self):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(
            api_key=Config.DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com",
        )
        self.model = Config.DEEPSEEK_MODEL
        logger_struct.info("deepseek_provider_initialized", model=self.model)

    async def _call(self, prompt: str) -> str:
        start = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                temperature=0.7,
            )
            text = response.choices[0].message.content
            if not text:
                raise LLMException("DeepSeek returned empty response")
            logger_struct.info("deepseek_response_generated",
                latency_ms=round((time.perf_counter() - start) * 1000, 2))
            return text
        except LLMException:
            raise
        except Exception as e:
            raise LLMException(f"DeepSeek error: {e}")

    @retry(stop=stop_after_attempt(RETRY_MAX_ATTEMPTS), wait=_gemini_wait,
           retry=retry_if_exception_type(Exception),
           before_sleep=before_sleep_log(logger_struct, logging.WARNING), reraise=True)
    async def generate(self, prompt: str) -> str:
        return await llm_circuit_breaker.call(self._call, prompt)

    async def generate_combined(self, prompt: str) -> dict:
        text = await self._call(prompt)
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            try:
                parsed = json.loads(json_match.group())
                if "response" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass
        return {"response": text, "summary": {}}

    async def generate_summary(self, prompt: str) -> str:
        return await summary_circuit_breaker.call(self._call, prompt)


class FallbackProvider(LLMProvider):
    """Tries providers in order, falling back on any exception."""

    def __init__(self, providers: list[LLMProvider]):
        self._providers = providers

    async def _with_fallback(self, method: str, *args) -> str:
        last_exc = None
        for provider in self._providers:
            try:
                return await getattr(provider, method)(*args)
            except Exception as exc:
                logger_struct.warning("fallback_provider_failed",
                    provider=type(provider).__name__, method=method, error=str(exc))
                last_exc = exc
        raise last_exc

    async def generate(self, prompt: str) -> str:
        return await self._with_fallback("generate", prompt)

    async def generate_combined(self, prompt: str) -> dict:
        return await self._with_fallback("generate_combined", prompt)

    async def generate_summary(self, prompt: str) -> str:
        return await self._with_fallback("generate_summary", prompt)

    async def generate_json(self, prompt: str) -> dict:
        return await self._with_fallback("generate_json", prompt)


# Module-level singleton — created once at first call, reused for every message.
_provider_instance: LLMProvider | None = None


def _build_provider(name: str) -> LLMProvider:
    name = name.lower()
    if name == "gemini":
        return GeminiProvider()
    elif name == "openai":
        return OpenAIProvider()
    elif name == "anthropic":
        return AnthropicProvider()
    elif name == "qwen":
        return QwenProvider()
    elif name == "deepseek":
        return DeepSeekProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {name}")


def get_llm_provider() -> LLMProvider:
    """Return the singleton LLM provider, initializing it on first call.

    Always wraps in FallbackProvider: primary → DeepSeek → Qwen.
    Fallbacks are skipped if their API key is not set.
    """
    global _provider_instance

    if _provider_instance is None:
        primary = _build_provider(Config.LLM_PROVIDER)

        fallbacks = []
        if Config.DEEPSEEK_API_KEY and Config.LLM_PROVIDER.lower() != "deepseek":
            fallbacks.append(DeepSeekProvider())
        if Config.OPENROUTER_API_KEY and Config.LLM_PROVIDER.lower() != "qwen":
            fallbacks.append(QwenProvider())

        if fallbacks:
            _provider_instance = FallbackProvider([primary] + fallbacks)
            logger_struct.info("llm_provider_singleton_created",
                provider=Config.LLM_PROVIDER,
                fallbacks=[type(f).__name__ for f in fallbacks])
        else:
            _provider_instance = primary
            logger_struct.info("llm_provider_singleton_created", provider=Config.LLM_PROVIDER)

    return _provider_instance
