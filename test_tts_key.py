import asyncio
import sys
sys.path.insert(0, '/app')
from src.tts.synthesizer import ElevenLabsSynthesizer

async def main():
    s = ElevenLabsSynthesizer()
    print(f"Testing with {len(s._api_keys)} key(s)")
    try:
        chunks = []
        async for c in s.synthesize("Hello, this is a test of the TTS system."):
            chunks.append(c)
        total = sum(len(c) for c in chunks)
        print(f"SUCCESS: {total:,} bytes generated")
    except Exception as e:
        print(f"FAILED: {e}")

asyncio.run(main())
