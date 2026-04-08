"""Test all ElevenLabs API keys to find working ones with detailed diagnostics.

Run this BEFORE starting docker compose to validate keys:
    python test_elevenlabs_keys.py

Or from inside the TTS container:
    docker compose exec tts python test_elevenlabs_keys.py
"""
import httpx
import asyncio
import os
import sys
from dotenv import load_dotenv

# Try loading from .env.docker first, then .env
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
    print("❌ No ElevenLabs API keys found in environment")
    print(f"   Checked: ELEVENLABS_API_KEY and ELEVENLABS_API_KEY1..20")
    print(f"   Loaded from: {env_file}")
    exit(1)

print(f"🔍 Testing {len(keys)} ElevenLabs API key(s)...\n")

async def test_key(idx: int, key: str):
    """Test a single API key with full diagnostics."""
    masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": key}
            )
            if resp.status_code == 200:
                data = resp.json()
                sub = data.get("subscription", {})
                tier = sub.get("tier", "unknown")
                chars_used = sub.get("character_count", 0)
                chars_limit = sub.get("character_limit", 0)
                remaining = chars_limit - chars_used
                print(f"✅ Key #{idx} ({masked}): WORKING")
                print(f"   Tier: {tier}")
                print(f"   Usage: {chars_used:,} / {chars_limit:,} characters")
                print(f"   Remaining: {remaining:,} characters")
                print(f"   Key type: {'FREE' if 'free' in tier.lower() else tier.upper()}")
                return {"valid": True, "tier": tier, "remaining": remaining}
            elif resp.status_code == 401:
                print(f"❌ Key #{idx} ({masked}): INVALID (401 Unauthorized)")
                print(f"   Response: {resp.text[:300]}")
                print(f"   Common causes: key revoked, expired, or IP-restricted")
                return {"valid": False, "error": "401", "detail": resp.text[:300]}
            elif resp.status_code == 402:
                print(f"⚠️  Key #{idx} ({masked}): NO QUOTA (402 Payment Required)")
                print(f"   Response: {resp.text[:300]}")
                return {"valid": False, "error": "402", "detail": resp.text[:300]}
            else:
                print(f"❌ Key #{idx} ({masked}): HTTP {resp.status_code}")
                print(f"   Response: {resp.text[:300]}")
                return {"valid": False, "error": f"http_{resp.status_code}", "detail": resp.text[:300]}
    except Exception as e:
        print(f"❌ Key #{idx} ({masked}): Error - {e}")
        return {"valid": False, "error": "exception", "detail": str(e)}

    return {"valid": False, "error": "unknown"}

async def test_synthesis(key: str, key_idx: int):
    """Test actual text-to-speech synthesis (only for valid keys)."""
    masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"

    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    output_format = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "pcm_22050")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format={output_format}"
    headers = {"xi-api-key": key, "Content-Type": "application/json"}
    body = {"text": "Hello, this is a test of the ElevenLabs TTS system.", "model_id": model_id}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code == 200:
                    total_bytes = 0
                    async for chunk in resp.aiter_bytes():
                        total_bytes += len(chunk)
                    print(f"   ✅ Synthesis test: {total_bytes:,} bytes generated")
                    print(f"      Model: {model_id}, Voice: {voice_id}")
                    return True
                else:
                    await resp.aread()
                    print(f"   ❌ Synthesis failed: HTTP {resp.status_code}")
                    print(f"      Response: {resp.text[:300]}")
                    if resp.status_code == 401:
                        print(f"      → Voice ID {voice_id} may not exist or model {model_id} not available on this tier")
                    return False
    except Exception as e:
        print(f"   ❌ Synthesis error: {e}")
        return False

async def main():
    results = await asyncio.gather(*[test_key(i+1, k) for i, k in enumerate(keys)])
    working = [r for r in results if r.get("valid")]
    failed = [r for r in results if not r.get("valid")]

    print(f"\n{'='*60}")
    print(f"📊 Results: {len(working)}/{len(keys)} keys valid\n")

    if working:
        print(f"✅ {len(working)} key(s) authenticated:\n")
        for i, r in enumerate(working):
            print(f"   Key #{i+1}: tier={r.get('tier', 'unknown')}, remaining={r.get('remaining', '?'):,} chars")

        # Test synthesis with first working key
        first_valid_idx = next(i for i, r in enumerate(results) if r.get("valid"))
        print(f"\n🔊 Testing synthesis with Key #{first_valid_idx + 1}...")
        print(f"   Model: {os.getenv('ELEVENLABS_MODEL_ID', 'eleven_flash_v2_5')}")
        print(f"   Voice: {os.getenv('ELEVENLABS_VOICE_ID', '21m00Tcm4TlvDq8ikWAM')}")
        success = await test_synthesis(keys[first_valid_idx], first_valid_idx + 1)
        if success:
            print(f"\n✅ Synthesis working — TTS should function correctly")
        else:
            print(f"\n⚠️  Synthesis failed — check model_id and voice_id compatibility")
    else:
        print(f"❌ NO VALID KEYS FOUND")
        print(f"\nTroubleshooting:")
        print(f"  1. Check your keys at: https://elevenlabs.io/settings/api")
        print(f"  2. Free tier keys may have expired or hit quota limits")
        print(f"  3. Ensure model_id '{os.getenv('ELEVENLABS_MODEL_ID', 'eleven_flash_v2_5')}' is available on your plan")
        print(f"  4. Some keys may be IP-restricted — check if running from a different machine")
        print(f"\nDetailed errors:")
        for i, r in enumerate(failed):
            print(f"  Key #{i+1}: {r.get('error', 'unknown')} — {r.get('detail', '')[:150]}")

    print(f"\n{'='*60}")

asyncio.run(main())
