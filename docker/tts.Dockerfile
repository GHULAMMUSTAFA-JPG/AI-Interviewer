FROM python:3.11-slim

# ── System deps ────────────────────────────────────────────────────────────────
# pulseaudio-utils: provides paplay — plays decoded PCM to PulseAudio.
# mpg123: lightweight MP3 decoder — decodes ElevenLabs MP3 stream to PCM.
# Together: ElevenLabs MP3 → mpg123 (decode) → paplay (play) → PulseAudio.
RUN apt-get update && apt-get install -y \
    pulseaudio-utils \
    mpg123 \
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
