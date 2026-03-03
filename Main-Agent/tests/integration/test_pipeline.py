"""
Integration tests for full pipeline.

Tests:
- End-to-end flow with mocked MongoDB and LLM
- Response saved correctly
- Latency within 900ms target
- Error handling and fallbacks
"""
import asyncio
import pytest
import pytest_asyncio
from datetime import datetime
from bson import ObjectId
from unittest.mock import AsyncMock, MagicMock, patch

from src.agent.pipeline import process_candidate_message
from src.agent.models import AgentOutput


@pytest.fixture
def mock_db():
    """Mock MongoDB database for pipeline tests"""
    db = MagicMock()

    # Mock all collections
    db.transcripts = MagicMock()
    db.interviews = MagicMock()
    db.candidates = MagicMock()
    db.jobs = MagicMock()
    db.companies = MagicMock()

    # Mock async methods
    db.transcripts.find_one = AsyncMock()
    db.transcripts.insert_one = AsyncMock()
    db.interviews.find_one = AsyncMock()
    db.interviews.update_one = AsyncMock()
    db.candidates.find_one = AsyncMock()
    db.jobs.find_one = AsyncMock()
    db.companies.find_one = AsyncMock()

    # Mock find method for recent messages
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.limit = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=[])
    db.transcripts.find = MagicMock(return_value=cursor)

    return db


@pytest.fixture
def sample_ids():
    """Generate sample ObjectIds for testing"""
    return {
        "transcript_id": str(ObjectId()),
        "interview_id": ObjectId(),
        "candidate_id": ObjectId(),
        "job_id": ObjectId(),
        "company_id": ObjectId()
    }


@pytest_asyncio.fixture
async def setup_pipeline_mocks(mock_db, sample_ids):
    """Setup all mocks for pipeline testing"""

    # Mock transcript
    mock_db.transcripts.find_one.return_value = {
        "_id": ObjectId(sample_ids["transcript_id"]),
        "interview_id": sample_ids["interview_id"],
        "speaker": "candidate",
        "text": "I have extensive experience with MongoDB and Python.",
        "timestamp": datetime.utcnow()
    }

    # Mock interview
    mock_db.interviews.find_one.return_value = {
        "_id": sample_ids["interview_id"],
        "candidate_id": sample_ids["candidate_id"],
        "job_id": sample_ids["job_id"],
        "status": "in_progress",
        "phase": "TECHNICAL",
        "turn_count": 5,
        "phase_turn_count": 1,
        "conversation_summary": "Candidate discussed backend development.",
        "started_at": datetime.utcnow()
    }

    # Mock candidate
    mock_db.candidates.find_one.return_value = {
        "_id": sample_ids["candidate_id"],
        "name": "Jane Smith",
        "email": "jane@example.com",
        "cv_text": "Senior Python Developer with 7 years of experience in backend systems. " * 50
    }

    # Mock job
    mock_db.jobs.find_one.return_value = {
        "_id": sample_ids["job_id"],
        "title": "Senior Backend Engineer",
        "description": "We are looking for a Senior Backend Engineer with Python and MongoDB expertise. " * 30,
        "company_id": sample_ids["company_id"]
    }

    # Mock company
    mock_db.companies.find_one.return_value = {
        "_id": sample_ids["company_id"],
        "name": "TechCorp Inc.",
        "info": "TechCorp is a fast-growing startup specializing in AI and machine learning. " * 10
    }

    # Mock insert/update responses
    mock_db.transcripts.insert_one.return_value = MagicMock(inserted_id=ObjectId())
    mock_db.interviews.update_one.return_value = MagicMock(modified_count=1)

    return mock_db


@pytest.mark.asyncio
async def test_pipeline_end_to_end_success(
    setup_pipeline_mocks,
    sample_ids
):
    """Test complete pipeline execution from trigger to save"""
    # Mock LLM provider to return a valid response
    with patch("src.agent.pipeline.get_llm_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(
            return_value="That's excellent experience! How did you handle schema migrations?"
        )
        mock_get_provider.return_value = mock_provider

        # Execute pipeline
        output = await process_candidate_message(
            setup_pipeline_mocks,
            sample_ids["transcript_id"]
        )

        # Verify output
        assert isinstance(output, AgentOutput)
        assert output.response_text is not None
        assert len(output.response_text) > 0
        assert output.phase == "TECHNICAL"
        assert output.is_fallback is False

        # Verify transcript was saved
        assert setup_pipeline_mocks.transcripts.insert_one.called

        # Verify interview was updated
        assert setup_pipeline_mocks.interviews.update_one.called


@pytest.mark.asyncio
async def test_pipeline_response_saved_correctly(
    setup_pipeline_mocks,
    sample_ids
):
    """Test that response is saved with correct structure"""
    with patch("src.agent.pipeline.get_llm_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(
            return_value="Can you describe your biggest technical challenge?"
        )
        mock_get_provider.return_value = mock_provider

        await process_candidate_message(setup_pipeline_mocks, sample_ids["transcript_id"])

        # Verify insert_one was called with correct structure
        call_args = setup_pipeline_mocks.transcripts.insert_one.call_args[0][0]

        assert call_args["interview_id"] == sample_ids["interview_id"]
        assert call_args["speaker"] == "agent"
        assert "technical challenge" in call_args["text"]
        assert "timestamp" in call_args
        assert "metadata" in call_args
        assert call_args["metadata"]["phase"] == "TECHNICAL"
        assert call_args["metadata"]["is_fallback"] is False


@pytest.mark.asyncio
async def test_pipeline_turn_count_updated(
    setup_pipeline_mocks,
    sample_ids
):
    """Test that interview turn_count is incremented"""
    with patch("src.agent.pipeline.get_llm_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(return_value="What technologies did you use?")
        mock_get_provider.return_value = mock_provider

        await process_candidate_message(setup_pipeline_mocks, sample_ids["transcript_id"])

        # Verify update_one was called
        call_args = setup_pipeline_mocks.interviews.update_one.call_args

        # Check filter (first argument)
        assert call_args[0][0]["_id"] == sample_ids["interview_id"]

        # Check update (second argument)
        update_doc = call_args[0][1]
        assert "$inc" in update_doc
        assert update_doc["$inc"]["turn_count"] == 1
        assert "$set" in update_doc
        assert "updated_at" in update_doc["$set"]


@pytest.mark.asyncio
async def test_pipeline_llm_failure_uses_fallback(
    setup_pipeline_mocks,
    sample_ids
):
    """Test that LLM failures trigger fallback responses when fallbacks enabled"""
    with patch("src.agent.pipeline.get_llm_provider") as mock_get_provider, \
         patch("src.agent.pipeline.ENABLE_FALLBACK_RESPONSES", True):
        # Mock LLM to raise LLMException
        from src.exceptions import LLMException
        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(side_effect=LLMException("LLM service unavailable"))
        mock_get_provider.return_value = mock_provider

        output = await process_candidate_message(setup_pipeline_mocks, sample_ids["transcript_id"])

        # Should use fallback
        assert output.is_fallback is True
        assert output.response_text is not None
        assert len(output.response_text) > 0


@pytest.mark.asyncio
async def test_pipeline_validation_failure_uses_fallback(
    setup_pipeline_mocks,
    sample_ids
):
    """Test that validation failures trigger fallback responses when fallbacks enabled"""
    with patch("src.agent.pipeline.get_llm_provider") as mock_get_provider, \
         patch("src.agent.pipeline.ENABLE_FALLBACK_RESPONSES", True):
        # Mock LLM to return invalid response (JSON)
        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(
            return_value='{"response": "This is JSON and should be rejected"}'
        )
        mock_get_provider.return_value = mock_provider

        output = await process_candidate_message(setup_pipeline_mocks, sample_ids["transcript_id"])

        # Should use fallback due to validation failure
        assert output.is_fallback is True


@pytest.mark.asyncio
async def test_pipeline_latency_within_target(
    setup_pipeline_mocks,
    sample_ids
):
    """Test that pipeline completes within 900ms target (with mocks)"""
    import time

    with patch("src.agent.pipeline.get_llm_provider") as mock_get_provider:
        # Simulate realistic LLM latency (200ms)
        async def mock_generate(prompt):
            await asyncio.sleep(0.2)  # 200ms
            return "How did you optimize your database queries?"

        mock_provider = AsyncMock()
        mock_provider.generate = mock_generate
        mock_get_provider.return_value = mock_provider

        start = time.time()
        await process_candidate_message(setup_pipeline_mocks, sample_ids["transcript_id"])
        elapsed = (time.time() - start) * 1000  # Convert to ms

        # With mocked DB and simulated LLM, should be <900ms
        assert elapsed < 900, f"Pipeline took {elapsed}ms (target <900ms)"


@pytest.mark.asyncio
async def test_pipeline_interview_completed_error(
    setup_pipeline_mocks,
    sample_ids
):
    """Test that completed interviews raise appropriate error"""
    # Mock interview as completed
    setup_pipeline_mocks.interviews.find_one.return_value["status"] = "completed"

    with pytest.raises(Exception):  # Should raise InterviewCompletedError
        await process_candidate_message(setup_pipeline_mocks, sample_ids["transcript_id"])


@pytest.mark.asyncio
async def test_pipeline_metadata_tracking(
    setup_pipeline_mocks,
    sample_ids
):
    """Test that metadata is tracked correctly"""
    with patch("src.agent.pipeline.get_llm_provider") as mock_get_provider:
        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(return_value="Tell me about your experience?")
        mock_get_provider.return_value = mock_provider

        await process_candidate_message(setup_pipeline_mocks, sample_ids["transcript_id"])

        call_args = setup_pipeline_mocks.transcripts.insert_one.call_args[0][0]
        metadata = call_args["metadata"]

        # Verify metadata fields
        assert "phase" in metadata
        assert "turn_count" in metadata
        assert "is_fallback" in metadata
        assert "llm_provider" in metadata
        assert "estimated_tokens" in metadata
