"""Prometheus metrics for TTS service. Exposed on METRICS_PORT (default 9002)."""
import os
from prometheus_client import Counter, Histogram, start_http_server

METRICS_PORT = int(os.getenv("METRICS_PORT", "9002"))

tts_synthesis_duration_seconds = Histogram(
    "tts_synthesis_duration_seconds",
    "Edge TTS synthesis + playback duration",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 20],
)

tts_processed_total = Counter(
    "tts_processed_total",
    "Total transcripts processed by TTS",
    ["status"],    # status: played | skipped_local | skipped_no_pa | error | stale
)

tts_mp3_bytes_total = Counter(
    "tts_mp3_bytes_total",
    "Total MP3 bytes synthesized by Edge TTS",
)


def start_metrics_server() -> None:
    try:
        start_http_server(METRICS_PORT)
        print(f"[METRICS] TTS metrics available at :{METRICS_PORT}/metrics")
    except OSError as e:
        print(f"[METRICS] Warning: could not start metrics server: {e}")
