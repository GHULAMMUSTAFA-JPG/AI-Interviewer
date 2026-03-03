"""
Polling version for local development (no replica set needed).

Use this instead of main.py for local testing without MongoDB replica set.
"""
import asyncio
from datetime import datetime, timedelta
from src.config import Config, MongoDB, logger
from src.agent.pipeline import process_candidate_message


async def main():
    """Poll for new candidate messages instead of using change streams"""
    try:
        Config.validate()
        await MongoDB.connect()
        db = MongoDB.get_db()

        logger.info("AI Interview Agent started (polling mode)")
        logger.info(f"Listening for candidate messages in {Config.MONGODB_DB}...")
        logger.info(f"Using LLM provider: {Config.LLM_PROVIDER}")

        # Track processed messages
        processed_ids = set()
        last_check = datetime.utcnow() - timedelta(minutes=5)

        while True:
            # Find new candidate messages since last check
            new_messages = await db.transcripts.find({
                "speaker": "candidate",
                "timestamp": {"$gte": last_check}
            }).sort("timestamp", 1).to_list(length=100)

            for msg in new_messages:
                msg_id = str(msg["_id"])

                if msg_id not in processed_ids:
                    logger.info(f"\nNew candidate message received")
                    logger.info(f"   Transcript ID: {msg_id}")
                    logger.info(f"   Message: {msg['text'][:50]}...")

                    # Process message
                    try:
                        output = await process_candidate_message(db, msg_id)
                        logger.info(f"Response generated: {output.response_text[:100]}...")
                    except Exception as e:
                        logger.error(f"Failed to process {msg_id}: {e}")

                    processed_ids.add(msg_id)

            # Update last check time
            if new_messages:
                last_check = new_messages[-1]["timestamp"]

            # Wait before next poll
            await asyncio.sleep(2)  # Poll every 2 seconds

    except KeyboardInterrupt:
        logger.info("\nShutting down gracefully...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        await MongoDB.disconnect()
        logger.info("Agent stopped")


if __name__ == "__main__":
    asyncio.run(main())