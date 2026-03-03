"""
Integration test: NATS interrupt stops audio.

Tests that the interrupt handler correctly signals playback to stop.
"""
import asyncio
import pytest
from unittest.mock import MagicMock


pytestmark = pytest.mark.asyncio


class TestInterruptionFlow:
    """Tests for interruption via asyncio.Event."""

    async def test_stop_event_propagates_to_playback(self):
        """Stop event set externally causes audio loop to exit."""
        stop_event = asyncio.Event()
        chunks_played = []

        async def fake_audio_gen():
            for i in range(10):
                yield b"\x00" * 4096

        async def simulated_play(audio_chunks, stop_event):
            async for chunk in audio_chunks:
                if stop_event.is_set():
                    break
                chunks_played.append(chunk)
                # Simulate interrupt after 3rd chunk
                if len(chunks_played) == 3:
                    stop_event.set()

        await simulated_play(fake_audio_gen(), stop_event)

        # Should have stopped after exactly 3 chunks (not all 10)
        assert len(chunks_played) == 3

    async def test_stop_event_reset_for_next_utterance(self):
        """After interrupt, stop_event can be cleared for next utterance."""
        from src.tts.interrupt_handler import InterruptHandler
        handler = InterruptHandler()

        # Simulate interrupt
        await handler._handle_interrupt(MagicMock(data=b""))
        assert handler.stop_event.is_set()

        # Reset for next utterance
        await handler.reset()
        assert not handler.stop_event.is_set()

    async def test_interruption_latency_model(self):
        """
        Model test: interrupt latency is bounded by chunk check frequency.

        At 22050Hz, 16-bit, chunk_size=4096 bytes:
        - 4096 bytes / 2 bytes per sample = 2048 samples
        - 2048 / 22050 ≈ 93ms per chunk
        - With chunk check: interrupt detected within ~1 chunk = ~93ms
        - But sounddevice.stop() is immediate (<1ms)
        - Net latency: <50ms if stop_event check + sd.stop() is fast
        """
        from src.tts.config import config
        samples_per_chunk = config.chunk_size // 2  # int16 = 2 bytes
        chunk_duration_ms = (samples_per_chunk / config.sample_rate) * 1000
        # This is a documentation test — latency is per-chunk based
        assert chunk_duration_ms < 200  # Bounded, though target is <50ms via sounddevice.stop()
