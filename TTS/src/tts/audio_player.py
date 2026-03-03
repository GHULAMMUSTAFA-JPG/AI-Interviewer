"""pacat audio player for PulseAudio virtual mic.

Audio path:
  ElevenLabs PCM chunks
    → pacat stdin
    → PULSE_SERVER=unix:/var/run/pulse/native  (shared Docker volume → meeting-bot)
    → PulseAudio virtual_mic null-sink          (--device=virtual_mic)
    → virtual_mic.monitor
    → virtual_mic_source (default PulseAudio source)
    → Chrome getUserMedia()
    → Google Meet WebRTC
    → meeting participants hear the agent

pacat is a native PulseAudio tool — more reliable than sounddevice/PortAudio for
cross-container audio routing via a shared UNIX socket.
PULSE_SERVER is inherited from the Docker environment (set in docker-compose.yml).
"""
import asyncio
import logging
from typing import AsyncGenerator

from .config import config

logger = logging.getLogger(__name__)


class AudioPlayer:
    """Plays mono PCM audio via pacat → PulseAudio virtual_mic."""

    def __init__(self) -> None:
        self._is_playing = False

    async def play(
        self,
        audio_chunks: AsyncGenerator[bytes, None],
        stop_event: asyncio.Event,
    ) -> None:
        """
        Stream PCM chunks from an async generator through pacat → PulseAudio.

        pacat inherits PULSE_SERVER from the environment, so it connects to the
        meeting-bot container's PulseAudio via the shared pulse-socket volume.
        Audio is routed to virtual_mic (null-sink) which Chrome reads as its mic.
        """
        cmd = [
            "pacat",
            "--playback",
            f"--device={config.virtual_mic}",
            "--format=s16le",
            f"--rate={config.sample_rate}",
            f"--channels={config.channels}",
        ]

        logger.info(
            f"Opening pacat stream: sink={config.virtual_mic} "
            f"rate={config.sample_rate} channels={config.channels}"
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
        )

        self._is_playing = True
        bytes_written = 0

        try:
            async for chunk in audio_chunks:
                if stop_event.is_set():
                    logger.info("Interrupt detected during playback")
                    break

                if not chunk:
                    continue

                proc.stdin.write(chunk)
                await proc.stdin.drain()
                bytes_written += len(chunk)

            proc.stdin.close()
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
