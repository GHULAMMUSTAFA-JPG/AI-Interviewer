"""Test all ElevenLabs API keys to find working ones."""
import httpx
import asyncio
import os
from dotenv import load_dotenv

load_dotenv(".env.docker")

# Load all keys
keys = []
if key := os.getenv("ELEVENLABS_API_KEY", "").strip():
    keys.append(key)
for i in range(1, 21):
    if key := os.getenv(f"ELEVENLABS_API_KEY{i}", "").strip():
        keys.append(key)

if not keys:
    print("❌ No ElevenLabs API keys found in .env.docker")
    exit(1)

print(f"🔍 Testing {len(keys)} ElevenLabs API key(s)...\n")

async def test_key(idx: int, key: str):
    """Test a single API key by fetching user subscription info."""
    masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": key}
            )
            if resp.status_code == 200:
                data = resp.json()
                name = data.get("subscription", {}).get("tier", "unknown")
                chars_used = data.get("subscription", {}).get("character_count", 0)
                chars_limit = data.get("subscription", {}).get("character_limit", 0)
                print(f"✅ Key #{idx} ({masked}): WORKING")
                print(f"   Tier: {name}")
                print(f"   Usage: {chars_used:,} / {chars_limit:,} characters")
                remaining = chars_limit - chars_used
                print(f"   Remaining: {remaining:,} characters")
                return True
            elif resp.status_code == 401:
                print(f"❌ Key #{idx} ({masked}): INVALID (401 Unauthorized)")
                return False
            elif resp.status_code == 402:
                print(f"⚠️  Key #{idx} ({masked}): NO QUOTA (402 Payment Required)")
                return False
            else:
                print(f"❌ Key #{idx} ({masked}): HTTP {resp.status_code}")
                return False
    except Exception as e:
        print(f"❌ Key #{idx} ({masked}): Error - {e}")
        return False

async def main():
    results = await asyncio.gather(*[test_key(i+1, k) for i, k in enumerate(keys)])
    working = sum(results)
    print(f"\n{'='*50}")
    print(f"📊 Results: {working}/{len(keys)} keys working")
    if working == 0:
        print("\n❌ No working keys found. You need to add a valid ElevenLabs API key.")
    else:
        print(f"\n✅ {working} key(s) working! Update .env.docker with a working key.")

asyncio.run(main())
