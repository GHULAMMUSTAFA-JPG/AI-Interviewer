"""
Play audio stored in GridFS from a previous TTS synthesis run.

Lists the last 10 transcripts that have audio saved, lets you pick one,
downloads the PCM audio from GridFS, and plays it through your local speakers.

Usage:
    uv run python scripts/play_from_db.py
    uv run python scripts/play_from_db.py --list       # list only, no playback
    uv run python scripts/play_from_db.py --all        # list all, not just last 10
"""
import asyncio
import sys

import numpy as np
import sounddevice as sd
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket

sys.path.insert(0, ".")
from src.tts.config import config

LIST_ONLY = "--list" in sys.argv
FETCH_ALL = "--all" in sys.argv
LIMIT = 0 if FETCH_ALL else 10


async def run() -> None:
    client = AsyncIOMotorClient(config.mongodb_uri)
    db = client[config.mongodb_db]
    collection = db[config.transcripts_collection]
    bucket = AsyncIOMotorGridFSBucket(db, bucket_name="audio")

    # Find transcripts with a GridFS file ID in audio_url (not null, not "played")
    query = {"audio_url": {"$nin": [None, "played"]}}
    sort = [("timestamp", -1)]

    cursor = collection.find(query, sort=sort)
    if LIMIT:
        cursor = cursor.limit(LIMIT)

    docs = await cursor.to_list(length=LIMIT or 1000)

    if not docs:
        print("\nNo audio found in DB yet.")
        print("→ Make sure the TTS service has processed at least one transcript.")
        print("→ Trigger one: uv run python scripts/benchmark.py")
        client.close()
        return

    print(f"\nFound {len(docs)} transcript(s) with stored audio:\n")
    for i, doc in enumerate(docs):
        text = doc.get("text", "")
        preview = text[:70] + ("..." if len(text) > 70 else "")
        ts = doc.get("timestamp", "")
        print(f"  [{i}] {preview}")
        print(f"       id={doc['_id']}  ts={ts}  audio_url={doc['audio_url']}\n")

    if LIST_ONLY:
        client.close()
        return

    choice = input("Enter number to play (Enter = play #0, q = quit): ").strip()
    if choice.lower() == "q":
        client.close()
        return

    idx = int(choice) if choice.isdigit() else 0
    if idx >= len(docs):
        print(f"Invalid choice: {idx}")
        client.close()
        return

    chosen = docs[idx]
    audio_url = chosen["audio_url"]

    print(f"\nDownloading from GridFS: {audio_url} ...")
    try:
        file_id = ObjectId(audio_url)
        stream = await bucket.open_download_stream(file_id)
        audio_data = await stream.read()
    except Exception as exc:
        print(f"Failed to download audio: {exc}")
        client.close()
        return

    kb = len(audio_data) / 1024
    samples = len(audio_data) // 2  # int16 = 2 bytes per sample
    duration_s = samples / config.sample_rate

    print(f"Downloaded: {kb:.1f} KB  |  {duration_s:.2f}s  |  {config.sample_rate}Hz mono 16-bit")
    print(f"Playing on device {config.audio_device_id} ...\n")

    audio_array = np.frombuffer(audio_data, dtype=np.int16)
    sd.play(audio_array, samplerate=config.sample_rate, device=config.audio_device_id)
    sd.wait()

    print("Playback complete.")
    client.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
