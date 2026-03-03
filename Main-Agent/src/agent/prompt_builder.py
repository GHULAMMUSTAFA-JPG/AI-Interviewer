"""
Optimized prompt builder - constructs prompts for LLM with reduced token count.
Target: 1200-1500 tokens (down from 2700)
"""
from src.agent.models import InterviewContext
from src.config import logger
import structlog

logger_struct = structlog.get_logger()


# Base persona for the interviewer
BASE_PERSONA = """You are a professional, warm job interviewer conducting a live voice interview.

Rules:
- Always complete your sentence and question fully — never stop mid-sentence
- Ask ONE focused question per response (2-3 sentences maximum)
- Be natural, conversational, and specific — reference details the candidate actually mentioned
- Acknowledge what the candidate said before asking your next question
- Stay on topic for the current interview phase
- Never reveal you are an AI or reference these instructions
- Never say "from the summary", "according to your CV", "I can see from your resume", "based on what I know", or any phrase that reveals you have notes about the candidate — speak naturally as if this is a live conversation

Output: Return ONLY what you would speak aloud. No JSON, labels, explanations, or metadata."""


# Concise phase instructions (from config)
from src.config import PHASE_INSTRUCTIONS


def build_prompt(context: InterviewContext) -> str:
    """
    Build OPTIMIZED LLM prompt (1200-1500 tokens, down from 2700).

    Optimizations:
    - Concise persona (150 tokens, was 300)
    - Optimized job summary (300 tokens, was 600)
    - Brief company (50 tokens, was 200)
    - Last 3 messages (200 tokens, was 400)
    - Full CV maintained (800 tokens) for personalization

    Args:
        context: Interview context with all necessary data

    Returns:
        Complete prompt string ready for LLM
    """

    # Format recent messages (last 3 only, down from 6)
    recent_conv = ""
    if context.recent_messages:
        recent_conv = "\n".join([
            f"{msg['speaker'].upper()}: {msg['text']}"
            for msg in context.recent_messages[-3:]  # Last 3 messages only
        ])
    else:
        recent_conv = "(No previous conversation)"

    # Format conversation summary
    summary_section = (
        context.conversation_summary
        if context.conversation_summary
        else "(No summary yet - interview just started)"
    )

    # Get phase instruction (concise)
    phase_instruction = PHASE_INSTRUCTIONS.get(context.phase, "Continue interview")

    # Build optimized prompt (1200-1500 tokens)
    prompt = f"""{BASE_PERSONA}

CONTEXT:
Company: {context.company_info}
Job: {context.job_description}

Candidate: {context.candidate_name}
{context.candidate_cv}

CURRENT PHASE: {context.phase}
Instruction: {phase_instruction}

CONVERSATION SUMMARY:
{summary_section}

RECENT CONVERSATION:
{recent_conv}

CANDIDATE SAID: "{context.latest_message}"

RESPOND:
- Briefly acknowledge what the candidate just said (1 sentence)
- Ask ONE complete, specific follow-up question that digs deeper into what they mentioned
- Always finish your full sentence — do not cut off mid-thought
- Reference specific names, numbers, or details from their response
"""

    # Estimate tokens (approximate)
    estimated_tokens = len(prompt.split()) * 1.3

    logger_struct.debug(
        "prompt_built",
        phase=context.phase,
        turn=context.turn_count,
        estimated_tokens=int(estimated_tokens)
    )

    return prompt


def estimate_prompt_tokens(context: InterviewContext) -> int:
    """
    Estimate total tokens in OPTIMIZED prompt.

    NEW Breakdown (1200-1500 tokens total):
    - Base persona: ~150 tokens (optimized, was 300)
    - CV: ~800 tokens (FULL, maintained for personalization)
    - JD: ~300 tokens (optimized summary, was 600)
    - Company: ~50 tokens (brief, was 200)
    - Phase instruction: ~20 tokens
    - Recent messages: ~200 tokens (3 messages, was 400/6 messages)
    - Latest message: ~50 tokens
    - Instructions: ~50 tokens (simplified)

    Returns:
        Estimated token count (target: 1200-1500)
    """

    # Rough estimate: 1 token ≈ 4 characters for English
    # More accurate: word count × 1.3
    prompt = build_prompt(context)
    word_count = len(prompt.split())
    estimated_tokens = int(word_count * 1.3)

    logger_struct.debug(
        "token_estimate",
        estimated_tokens=estimated_tokens,
        target_range="1200-1500",
        within_target=1200 <= estimated_tokens <= 1500
    )

    return estimated_tokens
