"""
Conversation summary generator.

Updates summary every 5 turns to maintain context efficiently.
"""
from src.config import logger
from src.agent.llm_provider import get_llm_provider
from src.exceptions import LLMException


async def generate_summary(messages: list[dict], current_summary: str = "") -> str:
    """
    Generate conversation summary from recent messages.

    Args:
        messages: List of recent messages (last 10-15)
        current_summary: Existing summary to build upon

    Returns:
        New summary (max 500 chars)
    """
    if not messages:
        return current_summary

    # Build summary prompt
    messages_text = "\n".join([
        f"{msg['speaker'].upper()}: {msg['text']}"
        for msg in messages[-15:]  # Last 15 messages
    ])

    prompt = f"""Summarize this interview conversation in under 500 characters.

Previous summary:
{current_summary if current_summary else '(None - this is the start of the interview)'}

Recent conversation:
{messages_text}

Create a concise summary covering:
- Key skills and experience mentioned by candidate
- Notable strengths or achievements discussed
- Any concerns or gaps identified
- Current discussion topics

Summary (max 500 chars):"""

    try:
        llm = get_llm_provider()
        summary = await llm.generate_summary(prompt)

        # Enforce 500 char limit
        if len(summary) > 500:
            summary = summary[:497] + "..."

        logger.info(f"Generated summary: {len(summary)} chars")
        return summary

    except Exception as e:
        logger.error(f"Summary generation failed: {e}")
        # Return current summary on failure
        return current_summary


def should_update_summary(turn_count: int) -> bool:
    """Check if summary should be updated (every 5 turns)"""
    return turn_count > 0 and turn_count % 5 == 0