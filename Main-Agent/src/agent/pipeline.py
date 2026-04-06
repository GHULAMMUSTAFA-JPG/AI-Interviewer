"""
OPTIMIZED Pipeline orchestrator - coordinates the 5-stage agent pipeline.

Performance optimizations:
- In-memory context caching (0-5ms cache hits vs 50-100ms DB reads)
- Optimized prompts (1200-1500 tokens vs 2700)
- Performance metrics tracking
- Structured logging

Stages:
1. Load context (0-5ms cached, 50-100ms first time)
2. Build prompt (5-10ms)
3. Call LLM (280-400ms with new SDK)
4. Validate output (5ms)
5. Save response (async, non-blocking)

Target: <500ms total latency
"""
import asyncio
import time
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
import structlog

# Use optimized context loader with caching
from src.agent.context_loader_optimized import load_interview_context
from src.agent.prompt_builder import build_prompt, estimate_prompt_tokens
from src.agent.llm_provider import get_llm_provider
from src.agent.validator import validate_output, get_fallback_response
from src.agent.models import AgentOutput
from src.agent.summary_generator import should_update_summary
from src.agent.phases import should_advance_phase, get_next_phase, should_end_interview
from src.agent.evaluator import generate_evaluation
from src.agent.safeguards import check_interview_safeguards, truncate_candidate_message, get_graceful_closing_message
from src.config import Config, logger, ENABLE_FALLBACK_RESPONSES
from src.exceptions import (
    LLMException,
    OutputValidationError,
    InterviewCompletedError,
    SafeguardTriggeredError
)

logger_struct = structlog.get_logger()


async def _set_agent_status(interview_id: str, status: str) -> None:
    """Write agent status to Redis (fire-and-forget, never raises)."""
    import time as _time
    try:
        from src.redis_client import get_redis
        redis = await get_redis()
        await redis.hset(
            f"agent:{interview_id}:status",
            mapping={"status": status, "updated_at": str(_time.time())}
        )
    except Exception:
        pass  # Redis unavailable — degrade silently


def _build_combined_prompt(interview_prompt: str, current_summary: str) -> str:
    """Append a summary-update request to the interview prompt for a combined JSON response."""
    return (
        f"{interview_prompt}\n\n"
        "---\n"
        "Additionally, update the running conversation summary.\n\n"
        f"Current summary: {current_summary or '(none)'}\n\n"
        "Respond ONLY with valid JSON (no markdown fences):\n"
        '{"response": "<your interview question or statement>", '
        '"summary": "<updated running summary max 500 chars>"}'
    )


async def process_candidate_message(
    db: AsyncIOMotorDatabase,
    transcript_id: str
) -> AgentOutput:
    """
    Main pipeline - process one candidate message and generate agent response.

    5-Stage Pipeline:
    1. LOAD CONTEXT (50-100ms) - Fetch interview data from MongoDB
    2. BUILD PROMPT (10-20ms) - Structure prompt for LLM
    3. CALL LLM (100-500ms) - Generate response from Gemini
    4. VALIDATE (5-10ms) - Check output quality and security
    5. SAVE & SEND (50-100ms) - Save to MongoDB, send to TTS

    Args:
        db: MongoDB database instance
        transcript_id: ID of the transcript that triggered this

    Returns:
        AgentOutput with response and metadata

    Raises:
        Various exceptions if pipeline fails
    """
    pipeline_start_time = time.perf_counter()
    start_time = datetime.utcnow()

    try:
        # ===== STAGE 1: LOAD CONTEXT (OPTIMIZED with caching) =====
        stage1_start = time.perf_counter()
        logger_struct.info("stage1_loading_context", transcript_id=transcript_id)
        context = await load_interview_context(db, transcript_id)
        stage1_latency = (time.perf_counter() - stage1_start) * 1000
        logger_struct.debug("stage1_complete", latency_ms=round(stage1_latency, 2))

        # Check if interview is already completed or abandoned — skip silently
        if context.status in ("completed", "abandoned"):
            raise InterviewCompletedError(f"Interview {context.interview_id} already {context.status}")

        # Truncate excessively long candidate messages
        context.latest_message, was_truncated = truncate_candidate_message(context.latest_message)

        # Estimate token usage
        estimated_tokens = estimate_prompt_tokens(context)
        logger.info(f"Estimated prompt tokens: {estimated_tokens}")

        # Check safeguards
        interview = await db.interviews.find_one({"interview_id": context.interview_id})
        safeguard_status = await check_interview_safeguards(interview, estimated_tokens, db)

        # Log warnings
        for warning in safeguard_status.warnings:
            logger.warning(f"[SAFEGUARD] {warning}")

        # Handle forced end
        if not safeguard_status.should_continue:
            logger.warning(f"[SAFEGUARD] Forcing interview end: {safeguard_status.forced_end_reason}")

            # Generate graceful closing message
            response_text = get_graceful_closing_message(safeguard_status.forced_end_reason)

            # Save closing message
            await db.transcripts.insert_one({
                "interview_id": context.interview_id,
                "speaker": "agent",
                "text": response_text,
                "timestamp": datetime.utcnow(),
                "metadata": {"forced_end": True, "reason": safeguard_status.forced_end_reason}
            })

            # Mark interview as completed or abandoned
            new_status = "abandoned" if "abandon" in safeguard_status.actions else "completed"
            await db.interviews.update_one(
                {"interview_id": context.interview_id},
                {
                    "$set": {
                        "status": new_status,
                        "ended_at": datetime.utcnow(),
                        "limits.forced_end_reason": safeguard_status.forced_end_reason
                    }
                }
            )

            return AgentOutput(
                response_text=response_text,
                phase=context.phase,
                should_advance=False,
                is_fallback=True
            )

        # Pre-compute for stage 3 branching and stage 5 state updates
        new_turn_count = context.turn_count + 1
        needs_summary = should_update_summary(new_turn_count)
        new_summary = context.conversation_summary

        # ===== STAGE 2: BUILD PROMPT (OPTIMIZED to 1200-1500 tokens) =====
        stage2_start = time.perf_counter()
        logger_struct.info("stage2_building_prompt", phase=context.phase)
        prompt = build_prompt(context)
        stage2_latency = (time.perf_counter() - stage2_start) * 1000
        logger_struct.debug("stage2_complete", latency_ms=round(stage2_latency, 2))

        # ===== STAGE 3: CALL LLM =====
        # On summary turns: one JSON call returns both response + updated summary.
        # On regular turns: one plain call returns response only.
        await _set_agent_status(context.interview_id, "thinking")
        stage3_start = time.perf_counter()
        logger_struct.info("stage3_calling_llm", provider=Config.LLM_PROVIDER, combined=needs_summary)
        llm = get_llm_provider()

        try:
            if needs_summary:
                combined_prompt = _build_combined_prompt(prompt, context.conversation_summary)
                combined = await llm.generate_combined(combined_prompt)
                response_text = combined.get("response", "").strip()
                if not response_text:
                    raise LLMException("Combined call returned empty response field")
                _summary = combined.get("summary")
                if _summary:
                    new_summary = _summary[:500]
            else:
                response_text = await llm.generate(prompt)
            is_fallback = False
            stage3_latency = (time.perf_counter() - stage3_start) * 1000
            logger_struct.info("stage3_complete", latency_ms=round(stage3_latency, 2), is_fallback=False, combined=needs_summary)

        except LLMException as e:
            stage3_latency = (time.perf_counter() - stage3_start) * 1000
            logger_struct.error("stage3_llm_failed", error=str(e), latency_ms=round(stage3_latency, 2))
            if ENABLE_FALLBACK_RESPONSES:
                response_text = get_fallback_response(context.phase)
                is_fallback = True
            else:
                raise

        # ===== STAGE 4: VALIDATE OUTPUT =====
        stage4_start = time.perf_counter()
        logger_struct.info("stage4_validating")
        validation = validate_output(response_text)
        stage4_latency = (time.perf_counter() - stage4_start) * 1000
        logger_struct.debug("stage4_complete", latency_ms=round(stage4_latency, 2))

        if not validation.is_valid:
            logger.warning(f"Validation failed: {validation.errors}")
            if ENABLE_FALLBACK_RESPONSES:
                response_text = get_fallback_response(context.phase)
                is_fallback = True
            else:
                logger.warning("Fallback disabled — using LLM output despite validation warnings")

        # ===== STAGE 5: SAVE & SEND =====
        logger.info(f"[STAGE 5] Saving response and updating state")

        # Save agent response to transcripts
        await db.transcripts.insert_one({
            "interview_id": context.interview_id,
            "speaker": "agent",
            "text": response_text,
            "timestamp": datetime.utcnow(),
            "audio_url": None,  # TTS will populate this
            "metadata": {
                "phase": context.phase,
                "turn_count": new_turn_count,
                "is_fallback": is_fallback,
                "llm_provider": Config.LLM_PROVIDER,
                "estimated_tokens": estimated_tokens
            }
        })

        # Check phase transitions
        interview = await db.interviews.find_one({"interview_id": context.interview_id})
        phase_turn_count = interview.get("phase_turn_count", 0) + 1

        should_advance, advance_reason = should_advance_phase(
            context.phase,
            phase_turn_count,
            new_turn_count
        )

        new_phase = context.phase
        if should_advance:
            next_phase = get_next_phase(context.phase)
            if next_phase:
                new_phase = next_phase
                phase_turn_count = 0  # Reset for new phase
                logger.info(f"Phase transition: {context.phase} -> {new_phase}")

        # Check if interview should end
        interview_ended = should_end_interview(new_phase, phase_turn_count)
        new_status = "completed" if interview_ended else "in_progress"

        # Update interview state
        update_doc = {
            "$inc": {"turn_count": 1},
            "$set": {
                "updated_at": datetime.utcnow(),
                "conversation_summary": new_summary,
                "phase": new_phase,
                "phase_turn_count": phase_turn_count,
                "status": new_status
            }
        }

        if interview_ended:
            update_doc["$set"]["ended_at"] = datetime.utcnow()

        await db.interviews.update_one(
            {"interview_id": context.interview_id},
            update_doc
        )

        # Generate final evaluation if interview completed
        if interview_ended:
            logger.info(f"Interview completed - generating evaluation")
            all_messages = await db.transcripts.find(
                {"interview_id": context.interview_id}
            ).sort("timestamp", 1).to_list(length=None)

            evaluation = await generate_evaluation(
                context.candidate_cv,
                context.job_description,
                context.company_info,
                new_summary,
                all_messages
            )

            # Store evaluation in interview document
            await db.interviews.update_one(
                {"interview_id": context.interview_id},
                {"$set": {"evaluation": evaluation}}
            )

            logger.info(f"Evaluation complete: {evaluation['recommendation']} (score: {evaluation['score']})")

        # Mark agent as idle and write phase/turn to Redis for UI live view
        await _set_agent_status(context.interview_id, "idle")
        try:
            from src.redis_client import get_redis
            import time as _time
            redis = await get_redis()
            await redis.hset(
                f"interview:{context.interview_id}:state",
                mapping={
                    "phase": new_phase,
                    "turn_count": str(new_turn_count),
                    "status": new_status,
                    "updated_at": str(_time.time()),
                }
            )
        except Exception:
            pass

        # Build output
        output = AgentOutput(
            response_text=response_text,
            phase=new_phase,
            should_advance=should_advance,
            is_fallback=is_fallback
        )

        # Calculate total pipeline latency
        total_latency_ms = (time.perf_counter() - pipeline_start_time) * 1000

        # Log performance summary
        logger_struct.info(
            "pipeline_complete",
            total_latency_ms=round(total_latency_ms, 2),
            stage1_latency_ms=round(stage1_latency, 2),
            stage2_latency_ms=round(stage2_latency, 2),
            stage3_latency_ms=round(stage3_latency, 2),
            stage4_latency_ms=round(stage4_latency, 2),
            interview_id=context.interview_id,
            phase=new_phase,
            turn=new_turn_count,
            is_fallback=is_fallback,
            target_met=total_latency_ms < 500,
            response_preview=response_text[:50]
        )


        return output

    except Exception as e:
        total_latency_ms = (time.perf_counter() - pipeline_start_time) * 1000
        logger_struct.error(
            "pipeline_failed",
            error=str(e),
            error_type=type(e).__name__,
            latency_ms=round(total_latency_ms, 2),
            exc_info=True
        )
        # Best-effort: reset agent status back to idle on any failure
        try:
            interview_id_for_err = locals().get("context") and locals()["context"].interview_id
            if interview_id_for_err:
                await _set_agent_status(interview_id_for_err, "idle")
        except Exception:
            pass
        raise


async def send_to_tts(interview_id: str, response_text: str):
    """
    Send agent response to TTS service.

    NOTE: This is a placeholder. Implement when TTS service is ready.

    Args:
        interview_id: Interview ID
        response_text: Text to convert to speech
    """
    logger.info(f"[TTS] Would send to TTS: {response_text[:50]}...")

    # TODO: Implement actual TTS integration with aiohttp
    # Example:
    # async with aiohttp.ClientSession() as session:
    #     async with session.post(
    #         Config.TTS_SERVICE_URL,
    #         json={
    #             "interview_id": interview_id,
    #             "text": response_text,
    #             "speaker": "agent"
    #         }
    #     ) as response:
    #         result = await response.json()
    #         return result.get("audio_url")

    pass
