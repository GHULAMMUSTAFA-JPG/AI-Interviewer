"""Test all ElevenLabs API keys to find working ones with detailed diagnostics.

Run this BEFORE starting docker compose to validate keys:
    python test_elevenlabs_keys.py

Or from inside the TTS container:
    docker compose exec tts python test_elevenlabs_keys.py
"""
import httpx
import asyncio
import os
import json
from dotenv import load_dotenv

env_file = ".env.docker" if os.path.exists(".env.docker") else ".env"
load_dotenv(env_file)

# Load all keys
keys = []
if key := os.getenv("ELEVENLABS_API_KEY", "").strip():
    keys.append(key)
for i in range(1, 21):
    if key := os.getenv(f"ELEVENLABS_API_KEY{i}", "").strip():
        keys.append(key)

if not keys:
    print("No ElevenLabs API keys found in environment")
    print(f"   Checked: ELEVENLABS_API_KEY and ELEVENLABS_API_KEY1..20")
    print(f"   Loaded from: {env_file}")
    exit(1)

print(f"Testing {len(keys)} ElevenLabs API key(s)...\n")


async def test_key(idx: int, key: str):
    """Test a single API key by making a real TTS request."""
    masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    output_format = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "pcm_22050")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    params = {"output_format": output_format}
    headers = {"xi-api-key": key, "Content-Type": "application/json"}
    body = {"text": "Hello test.", "model_id": model_id}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json=body, params=params)
            status = resp.status_code

            if status == 200:
                audio_bytes = resp.content
                print(f"Key #{idx} ({masked}): WORKING")
                print(f"   Generated {len(audio_bytes):,} bytes of audio")
                print(f"   Model: {model_id}, Voice: {voice_id}")
                print(f"   Format: {output_format}")
                return {"valid": True, "bytes": len(audio_bytes), "model": model_id}

            # Error response
            try:
                detail = resp.json().get("detail", {})
                if isinstance(detail, dict):
                    detail_text = detail.get("message", json.dumps(detail))
                else:
                    detail_text = str(detail)
            except Exception:
                detail_text = resp.text[:200]

            if status == 401:
                print(f"Key #{idx} ({masked}): INVALID (401)")
                print(f"   Detail: {detail_text}")
                if "missing_permissions" in detail_text.lower():
                    print(f"   Fix: Create a new key at elevenlabs.io/settings/api with text-to-speech permission")
                return {"valid": False, "error": "401", "detail": detail_text}
            elif status == 402:
                print(f"Key #{idx} ({masked}): NO QUOTA (402)")
                print(f"   Detail: {detail_text}")
                return {"valid": False, "error": "402", "detail": detail_text}
            elif status == 404:
                print(f"Key #{idx} ({masked}): NOT FOUND (404)")
                print(f"   Detail: {detail_text}")
                print(f"   Check voice_id='{voice_id}' and model_id='{model_id}'")
                return {"valid": False, "error": "404", "detail": detail_text}
            else:
                print(f"Key #{idx} ({masked}): HTTP {status}")
                print(f"   Detail: {detail_text[:200]}")
                return {"valid": False, "error": f"http_{status}", "detail": detail_text[:200]}

    except httpx.ConnectTimeout:
        print(f"Key #{idx} ({masked}): Connection timeout")
        return {"valid": False, "error": "timeout", "detail": "Could not reach ElevenLabs API"}
    except Exception as e:
        print(f"Key #{idx} ({masked}): Error - {e}")
        return {"valid": False, "error": "exception", "detail": str(e)}


async def main():
    results = await asyncio.gather(*[test_key(i + 1, k) for i, k in enumerate(keys)])
    working = [r for r in results if r.get("valid")]
    failed = [r for r in results if not r.get("valid")]

    print(f"\n{'=' * 60}")
    print(f"Results: {len(working)}/{len(keys)} keys working\n")

    if working:
        for i, r in enumerate(working):
            key_idx = next(j for j, res in enumerate(results) if res.get("valid") and i == sum(1 for x in results[:j] if x.get("valid")))
            print(f"  Key #{key_idx + 1}: OK - {r['bytes']:,} bytes (model={r['model']})")

        if len(failed) > 0:
            print(f"\n{len(failed)} key(s) failed:")
            for i, r in enumerate(failed):
                print(f"  Error: {r.get('error')} - {r.get('detail', '')[:120]}")
    else:
        print("NO WORKING KEYS FOUND")
        print(f"\nTroubleshooting:")
        print(f"  1. Go to elevenlabs.io/settings/api")
        print(f"  2. Create a NEW API key (or regenerate an existing one)")
        print(f"  3. Ensure it has 'Text-to-Speech' permission")
        print(f"  4. Update ELEVENLABS_API_KEY in your .env file")
        print(f"\nCurrent config:")
        print(f"  Model: {os.getenv('ELEVENLABS_MODEL_ID', 'eleven_flash_v2_5')}")
        print(f"  Voice: {os.getenv('ELEVENLABS_VOICE_ID', '21m00Tcm4TlvDq8ikWAM')}")
        print(f"\nDetailed errors:")
        for i, r in enumerate(failed):
            print(f"  Key #{i + 1}: {r.get('error')} - {r.get('detail', '')[:150]}")

    print(f"\n{'=' * 60}")


asyncio.run(main())
