"""
Interview phase management.

Handles phase transitions: INTRO → EXPERIENCE → TECHNICAL → BEHAVIORAL → CLOSING
"""
from typing import Literal
from src.config import logger

PhaseType = Literal["INTRO", "EXPERIENCE", "TECHNICAL", "BEHAVIORAL", "CLOSING"]

# Phase configuration
PHASE_CONFIG = {
    "INTRO": {
        "min_turns": 2,
        "max_turns": 3,
        "next_phase": "EXPERIENCE",
        "objective": "Build rapport and explain the interview format"
    },
    "EXPERIENCE": {
        "min_turns": 3,
        "max_turns": 8,
        "next_phase": "TECHNICAL",
        "objective": "Explore the candidate's work history and achievements"
    },
    "TECHNICAL": {
        "min_turns": 4,
        "max_turns": 10,
        "next_phase": "BEHAVIORAL",
        "objective": "Assess technical skills and problem-solving abilities"
    },
    "BEHAVIORAL": {
        "min_turns": 3,
        "max_turns": 8,
        "next_phase": "CLOSING",
        "objective": "Evaluate soft skills and cultural fit"
    },
    "CLOSING": {
        "min_turns": 3,
        "max_turns": 6,
        "next_phase": None,  # Last phase
        "objective": "Wrap up the interview and address candidate questions"
    }
}


def should_advance_phase(
    current_phase: PhaseType,
    phase_turn_count: int,
    total_turn_count: int,
    ready_to_advance: bool | None = None,
) -> tuple[bool, str]:
    """
    Determine if interview should advance to next phase.

    6.1: Advances when BOTH phase_turn_count >= min_turns AND LLM signals
    ready_to_advance=True. Hard cap at max_turns as before.

    Args:
        current_phase: Current interview phase
        phase_turn_count: Turns spent in current phase
        total_turn_count: Total turns in interview
        ready_to_advance: LLM signal — True means content is sufficient to advance.

    Returns:
        (should_advance, reason)
    """
    config = PHASE_CONFIG[current_phase]

    # Always advance at hard cap
    if phase_turn_count >= config["max_turns"]:
        reason = f"Max turns ({config['max_turns']}) reached for {current_phase}"
        logger.info(f"Phase transition: {reason}")
        return True, reason

    # Don't advance before min turns
    if phase_turn_count < config["min_turns"]:
        return False, f"Min turns ({config['min_turns']}) not met"

    # Between min and max: advance only if LLM confirms content is sufficient.
    # If no signal available, stay in phase (conservative default).
    if ready_to_advance is True:
        reason = f"LLM signalled ready_to_advance at turn {phase_turn_count} of {current_phase}"
        logger.info(f"Phase transition: {reason}")
        return True, reason

    return False, "LLM not ready to advance yet"


def get_next_phase(current_phase: PhaseType) -> PhaseType | None:
    """Get the next phase after current one"""
    return PHASE_CONFIG[current_phase]["next_phase"]


def get_phase_objective(phase: PhaseType) -> str:
    """Get objective for given phase"""
    return PHASE_CONFIG[phase]["objective"]


def should_end_interview(
    current_phase: PhaseType,
    phase_turn_count: int,
    candidate_finished: bool | None = None,
) -> bool:
    """Check if interview should end (CLOSING phase complete).

    6.3: Only ends if candidate_finished=True OR hard cap at max_turns.
    Prevents bot from closing mid-candidate-sentence.
    """
    if current_phase != "CLOSING":
        return False

    config = PHASE_CONFIG["CLOSING"]

    # Hard cap — always end at max_turns regardless of signal
    if phase_turn_count >= config["max_turns"]:
        return True

    # Between min and max: only end if LLM confirms candidate is done
    if phase_turn_count >= config["min_turns"]:
        return candidate_finished is True

    return False
