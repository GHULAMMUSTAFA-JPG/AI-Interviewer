"""
End-to-end TTS benchmark.

Inserts a transcript document, waits for audio_url to be set (meaning
ElevenLabs synthesis + GridFS save is complete), and reports latency.

Usage:
    uv run python scripts/benchmark.py
    uv run python scripts/benchmark.py --text "Custom text to synthesize"
"""
import asyncio
import sys
import time
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, ".")
from src.tts.config import config

DEFAULT_TEXT = "Hello, this is a benchmark test of the TTS system response time."


async def run(text: str) -> None:
    client = AsyncIOMotorClient(config.mongodb_uri)
    db = client[config.mongodb_db]
    collection = db[config.transcripts_collection]

    doc = {
        "interview_id": "benchmark",
        "speaker": "agent",
        "text": text,
        "audio_url": None,
        "timestamp": datetime.now(timezone.utc),
    }

    print(f"\n[benchmark] Text       : {len(text)} chars")
    print(f'[benchmark] Text       : "{text[:60]}{"..." if len(text) > 60 else ""}"')
    print(f"[benchmark] MongoDB    : {config.mongodb_uri}")
    print(f"[benchmark] Inserting document...")

    t0 = time.monotonic()
    result = await collection.insert_one(doc)
    doc_id = result.inserted_id
    t_inserted = time.monotonic()

    print(f"[benchmark] Inserted   : {doc_id}  (+{(t_inserted - t0)*1000:.0f}ms)")
    print(f"[benchmark] Waiting for synthesis (timeout=60s)...")

    timeout = 60.0
    poll_interval = 0.05  # 50ms — fine-grained polling

    while time.monotonic() - t0 < timeout:
        updated = await collection.find_one({"_id": doc_id})
        if updated and updated.get("audio_url") is not None:
            t1 = time.monotonic()
            total_ms = (t1 - t0) * 1000
            insert_ms = (t_inserted - t0) * 1000
            synthesis_ms = total_ms - insert_ms

            print(f"\n[benchmark] ✓ Synthesis complete")
            print(f"[benchmark] ─────────────────────────────")
            print(f"[benchmark]  Insert latency   : {insert_ms:.0f}ms")
            print(f"[benchmark]  Synthesis + save : {synthesis_ms:.0f}ms")
            print(f"[benchmark]  Total latency    : {total_ms:.0f}ms")
            print(f"[benchmark]  Chars/second     : {len(text) / (total_ms / 1000):.1f}")
            print(f"[benchmark]  audio_url        : {updated['audio_url']}")
            client.close()
            return

        await asyncio.sleep(poll_interval)

    print(f"\n[benchmark] TIMEOUT: synthesis not complete after {timeout}s")
    print("[benchmark] Is the TTS service running? → uv run python -m src.tts.main")
    client.close()


def main() -> None:
    text = DEFAULT_TEXT
    if "--text" in sys.argv:
        idx = sys.argv.index("--text")
        if idx + 1 < len(sys.argv):
            text = sys.argv[idx + 1]

    asyncio.run(run(text))


if __name__ == "__main__":
    main()
