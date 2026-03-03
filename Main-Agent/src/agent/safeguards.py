"""
Interview safeguards - prevent runaway costs and handle edge cases.

Monitors:
- Interview duration (soft/hard limits)
- Context window usage
- Candidate abandonment (silence)
- Message length
"""
from datetime import datetime, timedelta
from src.agent.models import SafeguardStatus
from src.config import Config, logger


async def check_interview_safeguards(interview: dict, estimated_tokens: int, db=None) -> SafeguardStatus:
    """
    Check all safeguards and return status with warnings/actions.

    Args:
        interview: Interview document from MongoDB
        estimated_tokens: Estimated context tokens for current turn

    Returns:
        SafeguardStatus with warnings and actions
    """
    warnings = []
    actions = []
    should_continue = True
    forced_end_reason = None

    # 1. Duration monitoring
    started_at = interview.get("started_at")
    if started_at:
        duration_minutes = (datetime.utcnow() - started_at).total_seconds() / 60

        # Soft limit warning
        if duration_minutes >= Config.SOFT_DURATION_LIMIT_MINUTES:
            warnings.append(f"Interview duration: {duration_minutes:.0f} min (soft limit: {Config.SOFT_DURATION_LIMIT_MINUTES} min)")
            actions.append("suggest_wrap_up")

        # Hard limit enforcement
        if Config.HARD_DURATION_LIMIT_MINUTES > 0 and duration_minutes >= Config.HARD_DURATION_LIMIT_MINUTES:
            should_continue = False
            forced_end_reason = f"Hard duration limit reached ({duration_minutes:.0f} min)"
            logger.warning(f"[SAFEGUARD] Forcing end: {forced_end_reason}")

    # 2. Context window monitoring
    if estimated_tokens >= Config.CONTEXT_WARNING_TOKENS:
        warnings.append(f"Context tokens: {estimated_tokens} (warning threshold: {Config.CONTEXT_WARNING_TOKENS})")

    if estimated_tokens >= Config.CONTEXT_MAX_TOKENS:
        should_continue = False
        forced_end_reason = f"Context window exceeded ({estimated_tokens} tokens)"
        logger.warning(f"[SAFEGUARD] Forcing end: {forced_end_reason}")

    # 3. Abandonment detection (check last message timestamp, not interview updated_at)
    # NOTE: Abandonment should only check if there's actual conversation happening
    # For new interviews or testing, we skip this check
    if db is not None:
        # transcripts.interview_id is stored as the string UUID (from the interviews
        # document's "interview_id" field), NOT as the MongoDB ObjectId.
        last_message = await db.transcripts.find_one(
            {"interview_id": str(interview["interview_id"]), "speaker": "candidate"},
            sort=[("timestamp", -1)]
        )
        if last_message:
            silence_seconds = (datetime.utcnow() - last_message["timestamp"]).total_seconds()
            if silence_seconds >= Config.SILENCE_TIMEOUT_SECONDS:
                should_continue = False
                forced_end_reason = f"Candidate abandoned (silent for {silence_seconds / 60:.0f} min)"
                logger.warning(f"[SAFEGUARD] Marking abandoned: {forced_end_reason}")
                actions.append("mark_abandoned")

    # 4. Turn count limits (backup safety)
    turn_count = interview.get("turn_count", 0)
    if turn_count >= 200:  # Extreme safety limit
        warnings.append(f"Turn count very high: {turn_count}")
        if turn_count >= 500:
            should_continue = False
            forced_end_reason = f"Maximum turn count exceeded ({turn_count})"

    return SafeguardStatus(
        should_continue=should_continue,
        warnings=warnings,
        actions=actions,
        forced_end_reason=forced_end_reason
    )


def truncate_candidate_message(text: str, max_length: int = 1000) -> tuple[str, bool]:
    """
    Truncate excessively long candidate messages.

    Args:
        text: Candidate message text
        max_length: Maximum allowed length

    Returns:
        (truncated_text, was_truncated)
    """
    if len(text) <= max_length:
        return text, False

    truncated = text[:max_length] + "... [truncated]"
    logger.warning(f"Truncated candidate message from {len(text)} to {max_length} chars")
    return truncated, True


def get_graceful_closing_message(reason: str) -> str:
    """
    Generate graceful closing message based on forced end reason.

    Args:
        reason: Reason for forced end

    Returns:
        Natural closing message
    """
    if "duration" in reason.lower():
        return "We've had a great conversation today. Due to time constraints, let's wrap up. Do you have any final questions?"

    elif "context" in reason.lower() or "token" in reason.lower():
        return "We've covered a lot of ground in our discussion. Let me wrap up with any final thoughts you'd like to share?"

    elif "abandoned" in reason.lower() or "silent" in reason.lower():
        return "I haven't heard from you in a while. Feel free to reach out when you're ready to continue."

    elif "turn" in reason.lower():
        return "We've had an extensive conversation. Let's conclude our discussion. Thank you for your time!"

    else:
        return "Let's wrap up our conversation now. Thank you for your time today!"
