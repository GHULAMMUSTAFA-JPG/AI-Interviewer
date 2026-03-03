"""
Unit tests for output validator.

Tests:
- Length validation (<500 chars)
- JSON detection
- Prompt leak detection
- Multi-line rejection
- Metadata detection
"""
import pytest
from src.agent.validator import validate_output, get_fallback_response


class TestValidateOutput:
    """Test suite for output validation"""

    def test_valid_response(self):
        """Test that valid responses pass validation"""
        valid_text = "That's impressive! Can you tell me more about your experience with MongoDB?"

        result = validate_output(valid_text)

        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_empty_response_rejected(self):
        """Test that empty responses are rejected"""
        result = validate_output("")

        assert result.is_valid is False
        assert any("empty" in err.lower() for err in result.errors)

    def test_whitespace_only_rejected(self):
        """Test that whitespace-only responses are rejected"""
        result = validate_output("   \n\t  ")

        assert result.is_valid is False
        assert any("empty" in err.lower() for err in result.errors)

    def test_too_short_rejected(self):
        """Test that responses shorter than 10 chars are rejected"""
        result = validate_output("Hi there")  # 8 chars

        assert result.is_valid is False
        assert any("too short" in err.lower() for err in result.errors)

    def test_too_long_rejected(self):
        """Test that responses longer than 500 chars are rejected"""
        long_text = "This is a very long response. " * 20  # 620 chars, exceeds 500

        result = validate_output(long_text)

        assert result.is_valid is False
        assert any("too long" in err.lower() for err in result.errors)

    def test_json_detected(self):
        """Test that JSON responses are rejected"""
        json_responses = [
            '{"response": "Hello there"}',
            '[1, 2, 3]',
            '{"phase": "INTRO", "text": "Hi"}'
        ]

        for json_text in json_responses:
            result = validate_output(json_text)

            assert result.is_valid is False
            assert any("json" in err.lower() for err in result.errors)

    def test_prompt_leak_detected(self):
        """Test that genuine prompt leaks are detected"""
        # Only patterns that match the validator's FORBIDDEN_PATTERNS
        leak_patterns = [
            "As per my system prompt, I should...",
            "My instructions say to do this.",
            "I am an AI language model and I will help.",
            "As an AI, I cannot answer that.",
        ]

        for leak_text in leak_patterns:
            result = validate_output(leak_text)

            assert result.is_valid is False
            assert any("prompt leak" in err.lower() for err in result.errors), \
                f"Failed to detect leak in: {leak_text}"

    def test_metadata_detected(self):
        """Test that metadata indicators are rejected"""
        metadata_responses = [
            "Confidence: 0.95 - That's great!",
            "Reasoning: The candidate has strong skills",
            "Tags: technical, experienced",
            "Score: 8/10 for this answer"
        ]

        for meta_text in metadata_responses:
            result = validate_output(meta_text)

            assert result.is_valid is False
            assert any("metadata" in err.lower() for err in result.errors)

    def test_multiline_warning(self):
        """Test that multi-line responses generate warnings"""
        multiline_text = "Line 1\nLine 2\nLine 3\nLine 4"

        result = validate_output(multiline_text)

        # Should still be valid but with warnings
        assert result.is_valid is True  # No hard errors
        assert len(result.warnings) > 0
        assert any("multi-line" in warn.lower() for warn in result.warnings)

    def test_no_question_warning(self):
        """Test that responses without questions generate warnings"""
        statement_only = "That is very interesting and shows your expertise."

        result = validate_output(statement_only)

        # Should be valid but with warning
        assert result.is_valid is True
        assert len(result.warnings) > 0
        assert any("question" in warn.lower() for warn in result.warnings)

    def test_valid_with_question(self):
        """Test that valid responses with questions have no warnings about questions"""
        good_response = "That's interesting! What was your biggest challenge?"

        result = validate_output(good_response)

        assert result.is_valid is True
        # Should not have question-related warnings
        question_warnings = [w for w in result.warnings if "question" in w.lower()]
        assert len(question_warnings) == 0


class TestGetFallbackResponse:
    """Test suite for fallback responses"""

    def test_fallback_for_intro_phase(self):
        """Test fallback response for INTRO phase"""
        response = get_fallback_response("INTRO")

        assert len(response) > 0
        assert len(response) < 500  # Should be valid length
        # Should be appropriate for intro
        assert any(word in response.lower() for word in ["tell", "yourself", "about"])

    def test_fallback_for_experience_phase(self):
        """Test fallback response for EXPERIENCE phase"""
        response = get_fallback_response("EXPERIENCE")

        assert len(response) > 0
        assert "experience" in response.lower()

    def test_fallback_for_technical_phase(self):
        """Test fallback response for TECHNICAL phase"""
        response = get_fallback_response("TECHNICAL")

        assert len(response) > 0
        assert any(word in response.lower() for word in ["approach", "problem", "solve"])

    def test_fallback_for_behavioral_phase(self):
        """Test fallback response for BEHAVIORAL phase"""
        response = get_fallback_response("BEHAVIORAL")

        assert len(response) > 0
        assert any(word in response.lower() for word in ["example", "situation", "handled"])

    def test_fallback_for_closing_phase(self):
        """Test fallback response for CLOSING phase"""
        response = get_fallback_response("CLOSING")

        assert len(response) > 0
        assert any(word in response.lower() for word in ["questions", "role", "company"])

    def test_fallback_for_unknown_phase(self):
        """Test fallback response for unknown phase"""
        response = get_fallback_response("UNKNOWN")

        assert len(response) > 0
        # Should return generic fallback
        assert "more" in response.lower()

    def test_all_fallbacks_valid(self):
        """Test that all fallback responses pass validation"""
        phases = ["INTRO", "EXPERIENCE", "TECHNICAL", "BEHAVIORAL", "CLOSING", "UNKNOWN"]

        for phase in phases:
            fallback = get_fallback_response(phase)
            validation = validate_output(fallback)

            assert validation.is_valid is True, \
                f"Fallback for {phase} failed validation: {validation.errors}"
