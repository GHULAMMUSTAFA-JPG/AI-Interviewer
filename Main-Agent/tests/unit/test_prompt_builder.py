"""
Unit tests for prompt builder.

Tests:
- Cacheable section structure (CV+JD+Company+Persona)
- Dynamic section (summary+messages+current)
- Token estimation accuracy
"""
import pytest
from datetime import datetime, UTC
from src.agent.prompt_builder import build_prompt, estimate_prompt_tokens, BASE_PERSONA
from src.agent.models import InterviewContext


@pytest.fixture
def sample_context():
    """Sample interview context for testing"""
    return InterviewContext(
        interview_id="507f1f77bcf86cd799439011",
        candidate_id="507f1f77bcf86cd799439012",
        job_id="507f1f77bcf86cd799439013",
        phase="TECHNICAL",
        turn_count=5,
        status="in_progress",
        started_at=datetime.now(UTC),
        candidate_name="John Doe",
        candidate_cv="Experienced Python developer with 5 years of backend experience. " * 50,  # ~1,700 tokens
        job_description="Looking for a Senior Backend Engineer with Python and MongoDB expertise. " * 30,  # ~900 tokens
        company_info="TechCorp is a fast-growing startup in the AI space. " * 10,  # ~350 tokens
        conversation_summary="Candidate discussed their experience with microservices and MongoDB.",
        recent_messages=[
            {"speaker": "candidate", "text": "I have worked with MongoDB for 3 years."},
            {"speaker": "agent", "text": "What was your biggest challenge?"},
            {"speaker": "candidate", "text": "Scaling to 10M users was challenging."},
        ],
        latest_message="We used sharding and replica sets to handle the load."
    )


def test_build_prompt_structure(sample_context):
    """Test that prompt has correct structure with all sections"""
    prompt = build_prompt(sample_context)

    # Check that prompt contains base persona
    assert BASE_PERSONA in prompt

    # Check that CV, JD, Company are included (cacheable section)
    assert sample_context.candidate_cv in prompt
    assert sample_context.job_description in prompt
    assert sample_context.company_info in prompt

    # Check that dynamic context is included
    assert sample_context.phase in prompt
    assert sample_context.conversation_summary in prompt
    assert sample_context.latest_message in prompt

    # Check that recent messages are formatted correctly
    for msg in sample_context.recent_messages:
        assert msg["text"] in prompt


def test_build_prompt_phase_objective(sample_context):
    """Test that prompt includes phase-specific objective"""
    prompt = build_prompt(sample_context)

    # Should include phase and objective
    assert "TECHNICAL" in prompt
    assert "Assess technical skills" in prompt or "problem-solving" in prompt


def test_build_prompt_recent_messages_limit():
    """Test that only last 3 messages are included"""
    context = InterviewContext(
        interview_id="507f1f77bcf86cd799439011",
        candidate_id="507f1f77bcf86cd799439012",
        job_id="507f1f77bcf86cd799439013",
        phase="INTRO",
        turn_count=10,
        status="in_progress",
        started_at=datetime.now(UTC),
        candidate_name="John Doe",
        candidate_cv="Test CV",
        job_description="Test JD",
        company_info="Test Company",
        conversation_summary="Test summary",
        recent_messages=[
            {"speaker": "candidate", "text": f"Message {i}"}
            for i in range(10)  # 10 messages (indices 0-9)
        ],
        latest_message="Latest message"
    )

    prompt = build_prompt(context)

    # Should only include last 3 messages (indices 7, 8, 9)
    assert "Message 7" in prompt  # Index 7 is in last 3
    assert "Message 0" not in prompt  # Index 0 is not in last 3


def test_build_prompt_empty_summary():
    """Test that prompt handles empty summary gracefully"""
    context = InterviewContext(
        interview_id="507f1f77bcf86cd799439011",
        candidate_id="507f1f77bcf86cd799439012",
        job_id="507f1f77bcf86cd799439013",
        phase="INTRO",
        turn_count=1,
        status="in_progress",
        started_at=datetime.now(UTC),
        candidate_name="John Doe",
        candidate_cv="Test CV",
        job_description="Test JD",
        company_info="Test Company",
        conversation_summary="",  # Empty summary
        recent_messages=[],
        latest_message="Hello"
    )

    prompt = build_prompt(context)

    # Should have placeholder for empty summary
    assert "(No summary yet" in prompt or "interview just started" in prompt


def test_estimate_prompt_tokens_accuracy(sample_context):
    """Test that token estimation is reasonably accurate"""
    estimated = estimate_prompt_tokens(sample_context)

    # The estimator uses word_count * 1.3; fixture has ~1100 words → ~1430 tokens
    assert 1200 < estimated < 3000, f"Expected ~1400 tokens for test fixture, got {estimated}"


def test_estimate_prompt_tokens_breakdown():
    """Test token estimation returns a positive integer value"""
    # The estimator uses word_count * 1.3; use real words so word-splitting is accurate
    context = InterviewContext(
        interview_id="507f1f77bcf86cd799439011",
        candidate_id="507f1f77bcf86cd799439012",
        job_id="507f1f77bcf86cd799439013",
        phase="INTRO",
        turn_count=1,
        status="in_progress",
        started_at=datetime.now(UTC),
        candidate_name="John Doe",
        candidate_cv=" ".join(["developer"] * 500),    # ~500 words
        job_description=" ".join(["engineer"] * 300),  # ~300 words
        company_info=" ".join(["startup"] * 100),      # ~100 words
        conversation_summary=" ".join(["discussed"] * 50),  # ~50 words
        recent_messages=[
            {"speaker": "candidate", "text": " ".join(["experience"] * 30)}
        ],
        latest_message=" ".join(["sharding"] * 20)
    )

    estimated = estimate_prompt_tokens(context)

    # ~1000+ content words + ~200 template words → well above minimum
    assert estimated > 1000
    assert isinstance(estimated, int)


def test_build_prompt_no_recent_messages():
    """Test prompt building with no conversation history"""
    context = InterviewContext(
        interview_id="507f1f77bcf86cd799439011",
        candidate_id="507f1f77bcf86cd799439012",
        job_id="507f1f77bcf86cd799439013",
        phase="INTRO",
        turn_count=0,
        status="in_progress",
        started_at=datetime.now(UTC),
        candidate_name="John Doe",
        candidate_cv="Test CV",
        job_description="Test JD",
        company_info="Test Company",
        conversation_summary="",
        recent_messages=[],  # No messages yet
        latest_message="Hello, I'm excited to interview!"
    )

    prompt = build_prompt(context)

    # Should indicate no previous conversation
    assert "(No previous conversation)" in prompt or "No summary yet" in prompt

    # Should still include latest message
    assert context.latest_message in prompt
