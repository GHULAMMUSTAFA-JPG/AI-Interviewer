"""
Interview evaluator - generates structured hiring decision after interview completion.
Uses Gemini JSON output mode for reliable structured data.
"""
from datetime import datetime
from src.config import logger
from src.agent.llm_provider import get_llm_provider


async def generate_evaluation(
    candidate_cv: str,
    job_description: str,
    company_info: str,
    conversation_summary: str,
    all_messages: list[dict]
) -> dict:
    interview_transcript = "\n".join([
        f"{msg['speaker'].upper()}: {msg['text']}"
        for msg in all_messages
    ])

    prompt = f"""You are an expert hiring manager. Analyze this job interview and return a JSON evaluation.

CANDIDATE CV:
{candidate_cv[:1500]}

JOB REQUIREMENTS:
{job_description[:1000]}

COMPANY:
{company_info[:400]}

INTERVIEW SUMMARY:
{conversation_summary}

FULL INTERVIEW TRANSCRIPT:
{interview_transcript[:4000]}

Return a JSON object with this exact structure. All fields are required.

{{
  "recommendation": "HIRE" or "MAYBE" or "NO_HIRE",
  "confidence": float 0.0-1.0 (how confident you are in your recommendation),
  "headline": "One sentence that captures the candidate — e.g. 'Strong backend engineer with gaps in system design'",
  "score": integer 1-10 (overall interview score),
  "competencies": [
    {{"name": "Technical Depth", "score": integer 1-5, "evidence": "Direct quote or specific example from the interview"}},
    {{"name": "Communication", "score": integer 1-5, "evidence": "Direct quote or specific example from the interview"}},
    {{"name": "Problem Solving", "score": integer 1-5, "evidence": "Direct quote or specific example from the interview"}},
    {{"name": "Cultural Fit", "score": integer 1-5, "evidence": "Direct quote or specific example from the interview"}},
    {{"name": "Experience Relevance", "score": integer 1-5, "evidence": "Direct quote or specific example from the interview"}}
  ],
  "strengths": ["strength 1", "strength 2", "strength 3"],
  "concerns": ["concern 1", "concern 2", "concern 3"],
  "red_flags": [],
  "reasoning": "2-3 sentences explaining the recommendation",
  "suggested_followups": ["Question to ask in a next-round interview", "Another follow-up question"],
  "phase_summaries": {{
    "INTRO": "What happened and what you learned in the intro phase",
    "EXPERIENCE": "Key experience points discussed",
    "TECHNICAL": "Technical assessment summary",
    "BEHAVIORAL": "Behavioral and soft skills observed",
    "CLOSING": "How the candidate wrapped up and any questions they asked"
  }}
}}"""

    try:
        llm = get_llm_provider()
        result = await llm.generate_json(prompt)
        evaluation = _validate_and_normalise(result)
        logger.info(
            f"Evaluation generated: {evaluation['recommendation']} "
            f"(score: {evaluation['score']}, confidence: {evaluation['confidence']:.0%})"
        )
        return evaluation

    except Exception as e:
        logger.error(f"Evaluation generation failed: {e}")
        return _fallback_evaluation()


def _validate_and_normalise(raw: dict) -> dict:
    """Ensure all expected fields exist and values are within bounds."""
    rec = str(raw.get("recommendation", "MAYBE")).upper()
    if rec not in ("HIRE", "MAYBE", "NO_HIRE"):
        rec = "MAYBE"

    score = raw.get("score", 5)
    try:
        score = max(1, min(10, int(score)))
    except (TypeError, ValueError):
        score = 5

    confidence = raw.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.5

    competencies = raw.get("competencies") or []
    normalised_competencies = []
    for c in competencies:
        if not isinstance(c, dict):
            continue
        c_score = c.get("score", 3)
        try:
            c_score = max(1, min(5, int(c_score)))
        except (TypeError, ValueError):
            c_score = 3
        normalised_competencies.append({
            "name": str(c.get("name", "Unknown")),
            "score": c_score,
            "evidence": str(c.get("evidence", "No evidence recorded")),
        })

    phase_summaries = raw.get("phase_summaries") or {}
    if not isinstance(phase_summaries, dict):
        phase_summaries = {}

    return {
        "recommendation": rec,
        "confidence": confidence,
        "headline": str(raw.get("headline", "")),
        "score": score,
        "competencies": normalised_competencies,
        "strengths": [str(s) for s in (raw.get("strengths") or [])],
        "concerns": [str(c) for c in (raw.get("concerns") or [])],
        "red_flags": [str(f) for f in (raw.get("red_flags") or [])],
        "reasoning": str(raw.get("reasoning", "")),
        "suggested_followups": [str(q) for q in (raw.get("suggested_followups") or [])],
        "phase_summaries": {
            k: str(v) for k, v in phase_summaries.items()
        },
        "generated_at": datetime.utcnow(),
    }


def _fallback_evaluation() -> dict:
    return {
        "recommendation": "MAYBE",
        "confidence": 0.0,
        "headline": "Evaluation could not be completed — manual review required.",
        "score": 5,
        "competencies": [],
        "strengths": ["Unable to evaluate — system error"],
        "concerns": ["Evaluation could not be completed"],
        "red_flags": [],
        "reasoning": "The interview evaluation failed due to a system error. Please review the transcript manually.",
        "suggested_followups": [],
        "phase_summaries": {},
        "generated_at": datetime.utcnow(),
    }
