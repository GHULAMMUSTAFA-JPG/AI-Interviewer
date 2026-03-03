"""
Interview evaluator - generates hiring decision after interview completion.
"""
from datetime import datetime
from src.config import logger
from src.agent.llm_provider import get_llm_provider
from src.exceptions import LLMException


async def generate_evaluation(
    candidate_cv: str,
    job_description: str,
    company_info: str,
    conversation_summary: str,
    all_messages: list[dict]
) -> dict:
    """
    Generate comprehensive hiring evaluation.

    Args:
        candidate_cv: Candidate's CV
        job_description: Job requirements
        company_info: Company information
        conversation_summary: Interview summary
        all_messages: All interview messages

    Returns:
        Evaluation dict with recommendation, score, strengths, concerns, reasoning
    """
    # Build context from messages
    interview_transcript = "\n".join([
        f"{msg['speaker'].upper()}: {msg['text']}"
        for msg in all_messages[-30:]  # Last 30 messages for context
    ])

    evaluation_prompt = f"""You are an expert hiring manager. Evaluate this interview and provide a hiring recommendation.

CANDIDATE CV:
{candidate_cv[:1000]}

JOB REQUIREMENTS:
{job_description[:800]}

COMPANY:
{company_info[:300]}

INTERVIEW SUMMARY:
{conversation_summary}

RECENT INTERVIEW TRANSCRIPT:
{interview_transcript[:2000]}

Provide a structured evaluation:

1. RECOMMENDATION: HIRE, MAYBE, or NO_HIRE
2. CONFIDENCE SCORE: 1-10 (how confident are you?)
3. KEY STRENGTHS: Top 3 strengths demonstrated
4. KEY CONCERNS: Top 3 concerns or gaps
5. REASONING: Brief explanation (2-3 sentences)

Format your response as:
RECOMMENDATION: [HIRE/MAYBE/NO_HIRE]
SCORE: [1-10]
STRENGTHS:
- [strength 1]
- [strength 2]
- [strength 3]
CONCERNS:
- [concern 1]
- [concern 2]
- [concern 3]
REASONING: [2-3 sentence explanation]
"""

    try:
        llm = get_llm_provider()
        evaluation_text = await llm.generate(evaluation_prompt)

        # Parse evaluation
        evaluation = parse_evaluation(evaluation_text)
        logger.info(f"Evaluation generated: {evaluation['recommendation']} (score: {evaluation['score']})")

        return evaluation

    except Exception as e:
        logger.error(f"Evaluation generation failed: {e}")
        return get_fallback_evaluation()


def parse_evaluation(text: str) -> dict:
    """Parse LLM evaluation response into structured dict"""
    lines = text.strip().split('\n')

    evaluation = {
        "recommendation": "MAYBE",  # Default
        "score": 5,
        "strengths": [],
        "concerns": [],
        "reasoning": "",
        "generated_at": datetime.utcnow()
    }

    current_section = None

    for line in lines:
        line = line.strip()

        if line.startswith("RECOMMENDATION:"):
            rec = line.split(":", 1)[1].strip().upper()
            if rec in ["HIRE", "MAYBE", "NO_HIRE"]:
                evaluation["recommendation"] = rec

        elif line.startswith("SCORE:"):
            try:
                score = int(line.split(":", 1)[1].strip())
                evaluation["score"] = max(1, min(10, score))  # Clamp 1-10
            except:
                pass

        elif line.startswith("STRENGTHS:"):
            current_section = "strengths"

        elif line.startswith("CONCERNS:"):
            current_section = "concerns"

        elif line.startswith("REASONING:"):
            evaluation["reasoning"] = line.split(":", 1)[1].strip()
            current_section = None

        elif line.startswith("-") and current_section:
            item = line.lstrip("- ").strip()
            if item:
                evaluation[current_section].append(item)

    return evaluation


def get_fallback_evaluation() -> dict:
    """Return fallback evaluation when LLM fails"""
    return {
        "recommendation": "MAYBE",
        "score": 5,
        "strengths": ["Unable to evaluate - system error"],
        "concerns": ["Evaluation could not be completed"],
        "reasoning": "The interview evaluation could not be generated due to a system error. Manual review recommended.",
        "generated_at": datetime.utcnow()
    }
