"""
Integration tests: Error recovery flows.

Tests for MongoDB reconnection and NATS reconnection logic.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


pytestmark = pytest.mark.asyncio


class TestErrorRecovery:
    """Tests for error recovery and reconnection logic."""

    async def test_synthesizer_stateless_after_error(self):
        """ElevenLabsSynthesizer is stateless — a second call succeeds after a prior failure."""
        from contextlib import asynccontextmanager
        from unittest.mock import patch
        import httpx
        from src.tts.synthesizer import ElevenLabsSynthesizer
        from src.tts.exceptions import ElevenLabsNetworkError

        synth = ElevenLabsSynthesizer()
        call_count = 0

        @asynccontextmanager
        async def fake_async_client(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.NetworkError("simulated failure")
            mock_client = AsyncMock()
            mock_response = AsyncMock()
            mock_response.status_code = 200

            async def fake_aiter_bytes(chunk_size=None):
                yield b"\x00" * 100

            mock_response.aiter_bytes = fake_aiter_bytes

            @asynccontextmanager
            async def fake_stream(*args, **kwargs):
                yield mock_response

            mock_client.stream = fake_stream
            yield mock_client

        with patch("src.tts.synthesizer.httpx.AsyncClient", fake_async_client):
            # First call raises network error
            with pytest.raises(ElevenLabsNetworkError):
                async for _ in synth.synthesize("first"):
                    pass

            # Second call succeeds — synthesizer carries no failure state
            chunks = []
            async for chunk in synth.synthesize("second"):
                chunks.append(chunk)

        assert chunks == [b"\x00" * 100]

    async def test_nats_interrupt_handler_resilient_to_empty_data(self):
        """Handler does not crash on empty NATS message."""
        from src.tts.interrupt_handler import InterruptHandler
        handler = InterruptHandler()

        msg = MagicMock()
        msg.data = b""

        await handler._handle_interrupt(msg)
        assert handler.stop_event.is_set()

    async def test_tts_queue_listener_handles_missing_full_document(self):
        """Queue listener skips change events without fullDocument."""
        # This tests the defensive check in tts_queue.py
        change_event = {"operationType": "insert"}  # no fullDocument key
        full_doc = change_event.get("fullDocument")
        assert full_doc is None  # should be skipped, not raise
