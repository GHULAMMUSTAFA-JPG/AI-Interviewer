"""Unit tests: ElevenLabs TTS synthesizer."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, AsyncMock
from contextlib import asynccontextmanager

import httpx


class TestElevenLabsSynthesizer:
    """Tests for ElevenLabsSynthesizer httpx streaming."""

    @pytest.fixture
    def synthesizer(self):
        from src.tts.synthesizer import ElevenLabsSynthesizer
        return ElevenLabsSynthesizer()

    @pytest.mark.asyncio
    async def test_close_is_noop(self, synthesizer):
        """close() should not raise — no persistent resources."""
        await synthesizer.close()

    @pytest.mark.asyncio
    async def test_synthesize_yields_chunks(self, synthesizer):
        """synthesize() yields bytes chunks from streaming response."""
        fake_chunks = [b"\x00" * 4096, b"\x01" * 2048]

        mock_response = AsyncMock()
        mock_response.status_code = 200

        async def fake_aiter_bytes(chunk_size=None):
            for chunk in fake_chunks:
                yield chunk

        mock_response.aiter_bytes = fake_aiter_bytes

        @asynccontextmanager
        async def fake_stream(*args, **kwargs):
            yield mock_response

        mock_client = AsyncMock()
        mock_client.stream = fake_stream

        @asynccontextmanager
        async def fake_async_client(*args, **kwargs):
            yield mock_client

        with patch("src.tts.synthesizer.httpx.AsyncClient", fake_async_client):
            collected = []
            async for chunk in synthesizer.synthesize("Hello"):
                collected.append(chunk)

        assert collected == fake_chunks

    @pytest.mark.asyncio
    async def test_synthesize_raises_auth_error_on_401(self, synthesizer):
        """401 response raises ElevenLabsAuthError."""
        from src.tts.exceptions import ElevenLabsAuthError

        mock_response = AsyncMock()
        mock_response.status_code = 401

        @asynccontextmanager
        async def fake_stream(*args, **kwargs):
            yield mock_response

        mock_client = AsyncMock()
        mock_client.stream = fake_stream

        @asynccontextmanager
        async def fake_async_client(*args, **kwargs):
            yield mock_client

        with patch("src.tts.synthesizer.httpx.AsyncClient", fake_async_client):
            with pytest.raises(ElevenLabsAuthError):
                async for _ in synthesizer.synthesize("Hello"):
                    pass

    @pytest.mark.asyncio
    async def test_synthesize_raises_rate_limit_on_429(self, synthesizer):
        """429 response raises ElevenLabsRateLimitError."""
        from src.tts.exceptions import ElevenLabsRateLimitError

        mock_response = AsyncMock()
        mock_response.status_code = 429

        @asynccontextmanager
        async def fake_stream(*args, **kwargs):
            yield mock_response

        mock_client = AsyncMock()
        mock_client.stream = fake_stream

        @asynccontextmanager
        async def fake_async_client(*args, **kwargs):
            yield mock_client

        with patch("src.tts.synthesizer.httpx.AsyncClient", fake_async_client):
            with pytest.raises(ElevenLabsRateLimitError):
                async for _ in synthesizer.synthesize("Hello"):
                    pass

    @pytest.mark.asyncio
    async def test_synthesize_raises_server_error_on_500(self, synthesizer):
        """5xx response raises ElevenLabsServerError."""
        from src.tts.exceptions import ElevenLabsServerError

        mock_response = AsyncMock()
        mock_response.status_code = 503

        @asynccontextmanager
        async def fake_stream(*args, **kwargs):
            yield mock_response

        mock_client = AsyncMock()
        mock_client.stream = fake_stream

        @asynccontextmanager
        async def fake_async_client(*args, **kwargs):
            yield mock_client

        with patch("src.tts.synthesizer.httpx.AsyncClient", fake_async_client):
            with pytest.raises(ElevenLabsServerError):
                async for _ in synthesizer.synthesize("Hello"):
                    pass

    @pytest.mark.asyncio
    async def test_synthesize_raises_network_error_on_timeout(self, synthesizer):
        """httpx.TimeoutException is wrapped as ElevenLabsNetworkError."""
        from src.tts.exceptions import ElevenLabsNetworkError

        @asynccontextmanager
        async def fake_async_client(*args, **kwargs):
            raise httpx.TimeoutException("timeout")
            yield  # make it a generator

        with patch("src.tts.synthesizer.httpx.AsyncClient", fake_async_client):
            with pytest.raises(ElevenLabsNetworkError):
                async for _ in synthesizer.synthesize("Hello"):
                    pass


class TestRaiseForStatus:
    """Tests for the _raise_for_status helper."""

    def _make_response(self, status_code: int) -> MagicMock:
        r = MagicMock()
        r.status_code = status_code
        return r

    def test_401_raises_auth_error(self):
        from src.tts.synthesizer import _raise_for_status
        from src.tts.exceptions import ElevenLabsAuthError
        with pytest.raises(ElevenLabsAuthError):
            _raise_for_status(self._make_response(401))

    def test_429_raises_rate_limit(self):
        from src.tts.synthesizer import _raise_for_status
        from src.tts.exceptions import ElevenLabsRateLimitError
        with pytest.raises(ElevenLabsRateLimitError):
            _raise_for_status(self._make_response(429))

    def test_500_raises_server_error(self):
        from src.tts.synthesizer import _raise_for_status
        from src.tts.exceptions import ElevenLabsServerError
        with pytest.raises(ElevenLabsServerError):
            _raise_for_status(self._make_response(500))

    def test_200_does_not_raise(self):
        from src.tts.synthesizer import _raise_for_status
        _raise_for_status(self._make_response(200))  # should not raise
