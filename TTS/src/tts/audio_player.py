"""MP3 audio player for PulseAudio virtual mic.

Audio path:
  ElevenLabs MP3 chunks (mp3_44100_128)
    → mpg123 stdin (MP3 decoder)
    → paplay stdout (PCM playback)
    → PULSE_SERVER=unix:/var/run/pulse/native  (shared Docker volume → meeting-bot)
    → PulseAudio virtual_mic null-sink          (--device=virtual_mic)
    → virtual_mic.monitor
    → virtual_mic_source (default PulseAudio source)
    → Chrome getUserMedia()
    → Google Meet WebRTC
    → meeting participants hear the agent

mpg123 is a lightweight, fast MP3 decoder (~200KB).
paplay plays decoded PCM to the PulseAudio virtual_mic.
PULSE_SERVER is inherited from the Docker environment (set in docker-compose.yml).
"""
import asyncio
import logging
from typing import AsyncGenerator

from .config import config

logger = logging.getLogger(__name__)


class AudioPlayer:
    """Plays MP3 audio via mpg123 (decode) → paplay (play) → PulseAudio virtual_mic."""

    def __init__(self) -> None:
        self._is_playing = False

    async def play(
        self,
        audio_chunks: AsyncGenerator[bytes, None],
        stop_event: asyncio.Event,
    ) -> None:
        """
        Stream MP3 chunks from ElevenLabs, decode via mpg123, play via paplay.

        Pipeline:
          MP3 chunks → mpg123 --stdout - (decode to PCM)
                     → paplay --device=virtual_mic (play to PulseAudio)

        paplay inherits PULSE_SERVER from the environment, so it connects to the
        meeting-bot container's PulseAudio via the shared pulse-socket volume.
        Audio is routed to virtual_mic (null-sink) which Chrome reads as its mic.
        """
        # Shell pipeline: mpg123 reads MP3 from stdin, outputs PCM to stdout,
        # paplay reads PCM from stdin and plays it to PulseAudio.
        shell_cmd = (
            "mpg123 --stdout - | "
            f"paplay --device={config.virtual_mic} "
            "--format=s16le --rate=44100 --channels=1 --latency-msec=50"
        )

        logger.info(
            f"Opening audio pipeline: mpg123 → paplay "
            f"sink={config.virtual_mic} rate=44100 channels=1 format=mp3"
        )

        proc = await asyncio.create_subprocess_shell(
            shell_cmd,
            stdin=asyncio.subprocess.PIPE,
        )

        self._is_playing = True
        bytes_written = 0
        interrupted = False

        try:
            async for chunk in audio_chunks:
                if stop_event.is_set():
                    logger.info("Interrupt detected during playback")
                    interrupted = True
                    break

                if not chunk:
                    continue

                proc.stdin.write(chunk)
                await proc.stdin.drain()
                bytes_written += len(chunk)

            proc.stdin.close()

            if interrupted:
                # Kill pipeline immediately so buffered audio doesn't keep playing
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    proc.kill()
                logger.info(f"Playback interrupted ({bytes_written} bytes written)")
            else:
                await proc.wait()
                logger.info(f"Playback complete ({bytes_written} bytes written)")

        except Exception:
            raise

        finally:
            self._is_playing = False
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()

    def stop(self) -> None:
        """Playback is interrupted via stop_event checked inside play()."""
        self._is_playing = False
