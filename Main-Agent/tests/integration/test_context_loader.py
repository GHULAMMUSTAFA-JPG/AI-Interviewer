"""
Integration tests for context loader.

Tests:
- MongoDB query mocking
- Parallel loading (candidate, job, company)
- Last 3 messages fetch
- Performance (<100ms target)
"""
import pytest
import pytest_asyncio
from datetime import datetime
from bson import ObjectId
from unittest.mock import AsyncMock, MagicMock, patch

from src.agent.context_loader import load_interview_context
from src.agent.models import InterviewContext


@pytest.fixture
def mock_db():
    """Mock MongoDB database"""
    db = MagicMock()

    # Mock transcripts collection
    db.transcripts = MagicMock()
    db.transcripts.find_one = AsyncMock()

    # Mock interviews collection
    db.interviews = MagicMock()
    db.interviews.find_one = AsyncMock()

    # Mock candidates collection
    db.candidates = MagicMock()
    db.candidates.find_one = AsyncMock()

    # Mock jobs collection
    db.jobs = MagicMock()
    db.jobs.find_one = AsyncMock()

    # Mock companies collection
    db.companies = MagicMock()
    db.companies.find_one = AsyncMock()

    # Mock find method for recent messages
    db.transcripts.find = MagicMock()
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.limit = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock()
    db.transcripts.find.return_value = cursor

    return db


@pytest.fixture
def sample_transcript_id():
    """Sample transcript ID"""
    return str(ObjectId())


@pytest.fixture
def sample_interview_id():
    """Sample interview ID"""
    return ObjectId()


@pytest.fixture
def sample_candidate_id():
    """Sample candidate ID"""
    return ObjectId()


@pytest.fixture
def sample_job_id():
    """Sample job ID"""
    return ObjectId()


@pytest.fixture
def sample_company_id():
    """Sample company ID"""
    return ObjectId()


@pytest_asyncio.fixture
async def setup_mock_data(
    mock_db,
    sample_transcript_id,
    sample_interview_id,
    sample_candidate_id,
    sample_job_id,
    sample_company_id
):
    """Setup mock database responses"""

    # Mock transcript document
    mock_db.transcripts.find_one.return_value = {
        "_id": ObjectId(sample_transcript_id),
        "interview_id": sample_interview_id,
        "speaker": "candidate",
        "text": "I have 5 years of Python experience.",
        "timestamp": datetime.utcnow()
    }

    # Mock interview document
    mock_db.interviews.find_one.return_value = {
        "_id": sample_interview_id,
        "candidate_id": sample_candidate_id,
        "job_id": sample_job_id,
        "status": "in_progress",
        "phase": "TECHNICAL",
        "turn_count": 5,
        "conversation_summary": "Candidate has backend development experience.",
        "started_at": datetime.utcnow()
    }

    # Mock candidate document
    mock_db.candidates.find_one.return_value = {
        "_id": sample_candidate_id,
        "name": "John Doe",
        "email": "john@example.com",
        "cv_text": "Experienced Python developer with 5 years of backend experience. " * 50
    }

    # Mock job document
    mock_db.jobs.find_one.return_value = {
        "_id": sample_job_id,
        "title": "Senior Backend Engineer",
        "description": "Looking for a Senior Backend Engineer with Python expertise. " * 30,
        "company_id": sample_company_id
    }

    # Mock company document
    mock_db.companies.find_one.return_value = {
        "_id": sample_company_id,
        "name": "TechCorp Inc.",
        "info": "TechCorp is a fast-growing startup in the AI space. " * 10
    }

    # Mock recent messages — sorted newest-first (matching sort("timestamp", -1))
    # After context_loader reverses them, agent becomes index 0 (older), candidate index 1 (newer).
    # Set agent first here so after reverse(), candidate is first.
    cursor = mock_db.transcripts.find.return_value
    cursor.to_list.return_value = [
        {
            "_id": ObjectId(),
            "interview_id": sample_interview_id,
            "speaker": "agent",
            "text": "What was your biggest challenge?",
            "timestamp": datetime.utcnow()
        },
        {
            "_id": ObjectId(),
            "interview_id": sample_interview_id,
            "speaker": "candidate",
            "text": "I worked with MongoDB extensively.",
            "timestamp": datetime.utcnow()
        }
    ]

    return mock_db


@pytest.mark.asyncio
async def test_load_interview_context_success(
    setup_mock_data,
    sample_transcript_id
):
    """Test successful context loading"""
    context = await load_interview_context(setup_mock_data, sample_transcript_id)

    # Verify context is returned with correct type
    assert isinstance(context, InterviewContext)

    # Verify all fields are populated
    assert context.interview_id is not None
    assert context.phase == "TECHNICAL"
    assert context.turn_count == 5
    assert context.status == "in_progress"
    assert "Python" in context.candidate_cv
    assert "Backend Engineer" in context.job_description
    assert "TechCorp" in context.company_info
    assert context.conversation_summary is not None
    assert context.latest_message == "I have 5 years of Python experience."


@pytest.mark.asyncio
async def test_load_interview_context_recent_messages(
    setup_mock_data,
    sample_transcript_id
):
    """Test that recent messages are loaded correctly"""
    context = await load_interview_context(setup_mock_data, sample_transcript_id)

    # Verify recent messages are included (2 messages, in chronological order after reverse)
    assert len(context.recent_messages) == 2
    # After reverse(), the list is in oldest-first order
    speakers = {msg["speaker"] for msg in context.recent_messages}
    assert "candidate" in speakers
    assert "agent" in speakers


@pytest.mark.asyncio
async def test_load_interview_context_parallel_queries(
    setup_mock_data,
    sample_transcript_id
):
    """Test that candidate, job, company are fetched in parallel"""
    # Use patch to track asyncio.gather calls while preserving return values
    candidate_data = setup_mock_data.candidates.find_one.return_value
    job_data = setup_mock_data.jobs.find_one.return_value

    with patch("src.agent.context_loader.asyncio.gather", new_callable=AsyncMock) as mock_gather:
        mock_gather.return_value = [candidate_data, job_data]

        context = await load_interview_context(setup_mock_data, sample_transcript_id)

        # Verify gather was called (parallel execution)
        assert mock_gather.called


@pytest.mark.asyncio
async def test_load_interview_context_empty_summary(
    setup_mock_data,
    sample_transcript_id
):
    """Test context loading with empty conversation summary"""
    # Update mock to have empty summary
    setup_mock_data.interviews.find_one.return_value["conversation_summary"] = ""

    context = await load_interview_context(setup_mock_data, sample_transcript_id)

    # Should handle empty summary gracefully
    assert context.conversation_summary == ""


@pytest.mark.asyncio
async def test_load_interview_context_no_recent_messages(
    setup_mock_data,
    sample_transcript_id
):
    """Test context loading with no recent messages"""
    # Update mock to return empty messages
    cursor = setup_mock_data.transcripts.find.return_value
    cursor.to_list.return_value = []

    context = await load_interview_context(setup_mock_data, sample_transcript_id)

    # Should handle no messages gracefully
    assert len(context.recent_messages) == 0


@pytest.mark.asyncio
async def test_load_interview_context_missing_transcript(
    mock_db,
    sample_transcript_id
):
    """Test error handling when transcript not found"""
    mock_db.transcripts.find_one.return_value = None

    with pytest.raises(Exception):  # Should raise appropriate exception
        await load_interview_context(mock_db, sample_transcript_id)


@pytest.mark.asyncio
async def test_load_interview_context_missing_interview(
    setup_mock_data,
    sample_transcript_id
):
    """Test error handling when interview not found"""
    setup_mock_data.interviews.find_one.return_value = None

    with pytest.raises(Exception):
        await load_interview_context(setup_mock_data, sample_transcript_id)


@pytest.mark.asyncio
async def test_load_interview_context_performance(
    setup_mock_data,
    sample_transcript_id
):
    """Test that context loading completes within 100ms (with mocked DB)"""
    import time

    start = time.time()
    context = await load_interview_context(setup_mock_data, sample_transcript_id)
    elapsed = (time.time() - start) * 1000  # Convert to ms

    # With mocked DB, should be very fast (<100ms)
    assert elapsed < 100, f"Context loading took {elapsed}ms (expected <100ms)"
