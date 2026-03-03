
"""
Replay stored PCM audio from MongoDB GridFS — no ElevenLabs API call needed.

Usage:
    uv run python scripts/replay_audio.py                   # latest stored audio
    uv run python scripts/replay_audio.py --list            # show available transcripts
    uv run python scripts/replay_audio.py --id <transcript_id>  # replay specific doc
    uv run python scripts/replay_audio.py --device <N>      # override output device
    uv run python scripts/replay_audio.py --list-devices    # show audio output devices
"""
import argparse
import asyncio
import os
import sys

import numpy as np
import sounddevice as sd
from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "interviews")
TRANSCRIPTS_COLLECTION = os.getenv("TRANSCRIPTS_COLLECTION", "transcripts")
SAMPLE_RATE = 22050
CHUNK_SIZE = 4096


def list_devices() -> None:
    print("\nAvailable audio output devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            print(f"  {i:3d}: {d['name']}")
    print()


def play_pcm(audio_data: bytes, device_id: int | None) -> None:
    aligned = audio_data[: len(audio_data) - len(audio_data) % 2]
    data = np.frombuffer(aligned, dtype=np.int16)
    sd.play(data, samplerate=SAMPLE_RATE, device=device_id)
    sd.wait()


async def run(args: argparse.Namespace) -> None:
    client = AsyncIOMotorClient(MONGODB_URI)
    col = client[MONGODB_DB][TRANSCRIPTS_COLLECTION]
    bucket = AsyncIOMotorGridFSBucket(client[MONGODB_DB], bucket_name="audio")

    if args.list:
        # List transcripts that have a GridFS audio_url (24-char hex ObjectId)
        docs = await col.find(
            {"audio_url": {"$regex": "^[0-9a-f]{24}$"}},
            {"_id": 1, "interview_id": 1, "text": 1, "audio_url": 1},
        ).sort("timestamp", -1).limit(20).to_list(20)
        if not docs:
            print("No transcripts with stored audio found.")
        else:
            print(f"\n{'transcript _id':<28} {'interview_id':<28} {'gridfs id':<28}  text")
            print("-" * 110)
            for d in docs:
                text = d.get("text", "")[:50]
                print(f"{str(d['_id']):<28} {str(d.get('interview_id','')):<28} {d.get('audio_url',''):<28}  {text}")
        client.close()
        return

    # Find the transcript
    if args.id:
        try:
            oid = ObjectId(args.id)
        except Exception:
            oid = args.id
        doc = await col.find_one({"_id": oid})
    else:
        doc = await col.find_one(
            {"audio_url": {"$regex": "^[0-9a-f]{24}$"}},
            sort=[("timestamp", -1)],
        )

    if not doc:
        print("No transcript with stored audio found.")
        client.close()
        sys.exit(1)

    audio_url = doc.get("audio_url", "")
    if not audio_url or len(audio_url) != 24:
        print(f"Transcript {doc['_id']} has no GridFS audio (audio_url={audio_url!r}).")
        client.close()
        sys.exit(1)

    # Fetch from GridFS
    try:
        grid_out = await bucket.open_download_stream(ObjectId(audio_url))
        audio_data = await grid_out.read()
    except Exception as exc:
        print(f"Failed to fetch audio from GridFS: {exc}")
        client.close()
        sys.exit(1)

    client.close()

    device_id = args.device
    device_name = sd.query_devices(device_id)["name"] if device_id is not None else "system default"
    duration = len(audio_data) / (SAMPLE_RATE * 2)
    print(f"\nText:    {doc.get('text', '')[:80]}")
    print(f"Audio:   {len(audio_data) / 1024:.1f} KB PCM 22050 Hz  ({duration:.2f}s)")
    print(f"Device:  [{device_id}] {device_name}\n")

    play_pcm(audio_data, device_id)
    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay TTS audio from MongoDB GridFS")
    parser.add_argument("--list", action="store_true", help="List transcripts with stored audio")
    parser.add_argument("--id", help="Replay specific transcript by _id")
    parser.add_argument("--device", type=int, default=None, help="Output device ID")
    parser.add_argument("--list-devices", action="store_true", help="List audio output devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
