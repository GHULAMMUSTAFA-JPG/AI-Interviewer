"""
Pydantic models for the AI Interview Agent.
"""
from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime


class AgentInput(BaseModel):
    """Input data for agent pipeline"""

    # Identifiers
    interview_id: str
    candidate_id: str
    job_id: str

    # Primary trigger
    candidate_message: str

    # Short-term memory (last 6 messages)
    recent_messages: list[dict]

    # Long-term memory (compressed)
    conversation_summary: str = ""

    # Interview state
    phase: Literal["INTRO", "EXPERIENCE", "TECHNICAL", "BEHAVIORAL", "CLOSING"]
    turn_count: int

    # Static context
    candidate_cv: str
    job_description: str
    company_info: str
    required_skills: list[str] = Field(default_factory=list)

    # Behavioral flags
    candidate_silent: bool = False
    candidate_off_topic: bool = False
    is_retry: bool = False


class AgentOutput(BaseModel):
    """Output data from agent pipeline"""

    # Primary output
    response_text: str

    # State updates
    phase: Literal["INTRO", "EXPERIENCE", "TECHNICAL", "BEHAVIORAL", "CLOSING"]
    should_advance: bool

    # Metadata (internal use only)
    confidence: float | None = None
    reasoning_tags: list[str] = Field(default_factory=list)
    is_fallback: bool = False

    # Summary update (every 5 turns)
    conversation_summary: str | None = None


class InterviewContext(BaseModel):
    """Complete interview context loaded from MongoDB"""

    # Identifiers
    interview_id: str
    candidate_id: str = ""   # not used in flat schema
    job_id: str = ""         # not used in flat schema

    # Interview metadata
    phase: Literal["INTRO", "EXPERIENCE", "TECHNICAL", "BEHAVIORAL", "CLOSING"]
    turn_count: int
    status: Literal["scheduled", "in_progress", "completed", "abandoned"]
    started_at: datetime | None
    conversation_summary: str = ""

    # Static context (from references)
    candidate_name: str  # Added for prompt personalization
    candidate_cv: str
    job_description: str
    company_info: str
    required_skills: list[str] = Field(default_factory=list)
    experience_level: str = ""

    # Dynamic context
    recent_messages: list[dict]
    latest_message: str

    # Phase tracking (avoids extra find_one in Stage 5)
    phase_turn_count: int = 0

    # Safeguard tracking
    estimated_context_tokens: int = 0


class ValidationResult(BaseModel):
    """Result of output validation"""

    is_valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SafeguardStatus(BaseModel):
    """Status of interview safeguards"""

    should_continue: bool = True
    warnings: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    forced_end_reason: str | None = None
