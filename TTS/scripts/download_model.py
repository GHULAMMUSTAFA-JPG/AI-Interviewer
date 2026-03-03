#!/usr/bin/env python3
"""Download Piper TTS model on first run."""
import os
import sys
import urllib.request
from pathlib import Path

MODEL_DIR = Path("models/piper")
MODEL_NAME = "en_US-amy-medium"
MODEL_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main"
    "/en/en_US/amy/medium/en_US-amy-medium.onnx"
)
CONFIG_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main"
    "/en/en_US/amy/medium/en_US-amy-medium.onnx.json"
)


def download(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"  Already exists: {dest}")
        return
    print(f"  Downloading {dest.name}...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)
    print(f"  Saved to {dest}")


def main() -> None:
    print("Piper model download")
    download(MODEL_URL, MODEL_DIR / f"{MODEL_NAME}.onnx")
    download(CONFIG_URL, MODEL_DIR / f"{MODEL_NAME}.onnx.json")
    print("Done.")


if __name__ == "__main__":
    main()
