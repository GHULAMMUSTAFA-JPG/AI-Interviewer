FROM python:3.11-slim

# ── System deps ────────────────────────────────────────────────────────────────
# pulseaudio-utils: provides pacat — streams PCM audio directly to PulseAudio.
#                   More reliable than sounddevice/PortAudio for cross-container
#                   audio routing via the shared pulse-socket Docker volume.
RUN apt-get update && apt-get install -y \
    pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY src/ ./src/
COPY models/ ./models/

ENV PYTHONPATH=/app

CMD ["python", "-m", "src.tts.main"]
