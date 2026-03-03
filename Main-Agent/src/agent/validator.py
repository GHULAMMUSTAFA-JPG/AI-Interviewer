"""
Output validator - validates LLM responses for quality and security.
"""
import re
from src.agent.models import ValidationResult
from src.config import logger


# Forbidden patterns that indicate genuine prompt leaks or persona breaks.
# These must be specific enough to NOT match normal interviewer language.
FORBIDDEN_PATTERNS = [
    r"system prompt",
    r"ai language model",
    r"as an ai[,\s]",
    r"i('m| am) an? (ai|artificial intelligence|language model|chatbot|virtual assistant)\b",
    r"my (system |hidden |secret )?(instructions|programming|training) (say|tell|require|forbid)",
    r"i cannot (do that|assist|help).{0,30}(ai|language model|not (programmed|designed|trained))",
]


def validate_output(text: str) -> ValidationResult:
    """
    Validate LLM output for quality and security.

    Checks:
    - Required field presence
    - Length constraints
    - Format requirements
    - Security (prompt leaks, JSON, metadata)

    Args:
        text: LLM generated response text

    Returns:
        ValidationResult with is_valid flag and any errors/warnings
    """
    errors = []
    warnings = []

    # 1. Basic checks
    if not text or not text.strip():
        errors.append("Response is empty")
        return ValidationResult(is_valid=False, errors=errors)

    text_clean = text.strip()

    # 2. Length checks
    if len(text_clean) < 10:
        errors.append(f"Response too short: {len(text_clean)} chars (min: 10)")

    if len(text_clean) > 500:
        errors.append(f"Response too long: {len(text_clean)} chars (max: 500)")

    # 3. Security checks - prompt leaks
    text_lower = text_clean.lower()

    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, text_lower):
            errors.append(f"Potential prompt leak detected: '{pattern}'")

    # 4. Format checks - should not return JSON or structured data
    if text_clean.startswith("{") or text_clean.startswith("["):
        errors.append("Response appears to be JSON (should be plain text)")

    if text_clean.count("\n") > 2:
        warnings.append(f"Multi-line response detected ({text_clean.count(chr(10))} lines)")

    # 5. Check for metadata leakage
    metadata_indicators = ["confidence:", "reasoning:", "tags:", "score:"]
    for indicator in metadata_indicators:
        if indicator.lower() in text_lower:
            errors.append(f"Metadata detected in response: '{indicator}'")

    # 6. Quality checks
    # Check if response is actually asking a question (should be for interview)
    has_question = "?" in text_clean
    if not has_question and len(errors) == 0:
        warnings.append("Response doesn't contain a question (might be a statement)")

    # 7. Determine if valid
    is_valid = len(errors) == 0

    if not is_valid:
        logger.warning(f"Validation failed: {errors}")
    elif warnings:
        logger.debug(f"Validation warnings: {warnings}")

    return ValidationResult(
        is_valid=is_valid,
        errors=errors,
        warnings=warnings
    )


def get_fallback_response(phase: str = "INTRO") -> str:
    """
    Get a safe fallback response when LLM fails or validation fails.

    Args:
        phase: Current interview phase

    Returns:
        Safe fallback response appropriate for the phase
    """
    fallbacks = {
        "INTRO": "Thank you for joining! Could you start by telling me a bit about yourself?",
        "EXPERIENCE": "That's interesting. Can you tell me more about your experience?",
        "TECHNICAL": "I see. How would you approach solving that kind of problem?",
        "BEHAVIORAL": "Can you share an example of how you've handled a similar situation?",
        "CLOSING": "Do you have any questions for me about the role or the company?",
    }

    response = fallbacks.get(phase, "Please tell me more about that.")
    logger.info(f"Using fallback response for phase {phase}")

    return response
