FROM python:3.11-slim

# ── System deps ────────────────────────────────────────────────────────────────
# pulseaudio-utils : provides pacat — streams raw PCM to PulseAudio virtual_mic.
# mpg123           : MP3 → PCM decoder used by the Edge TTS path only.
#                    ElevenLabs outputs pcm_22050 directly so mpg123 is NOT used
#                    when TTS_PROVIDER=elevenlabs, but it is kept here so the
#                    image works with both providers without a rebuild.
RUN apt-get update && apt-get install -y --no-install-recommends \
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
