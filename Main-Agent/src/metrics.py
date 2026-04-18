"""
Prometheus metrics for Main-Agent.
Starts a lightweight HTTP server on METRICS_PORT (default 9001).
Scraped by Prometheus every 15s.
"""
import os
import threading
from prometheus_client import (
    Counter, Histogram, Gauge,
    start_http_server, REGISTRY
)

METRICS_PORT = int(os.getenv("METRICS_PORT", "9001"))

# ── Counters ────────────────────────────────────────────────────────────────
llm_calls_total = Counter(
    "llm_calls_total",
    "Total LLM API calls made",
    ["provider", "status"],          # status: success | error | rate_limited
)

interviews_completed_total = Counter(
    "interviews_completed_total",
    "Total interviews completed (evaluation generated)",
)

interviews_abandoned_total = Counter(
    "interviews_abandoned_total",
    "Total interviews abandoned before completion",
)

circuit_breaker_opens_total = Counter(
    "circuit_breaker_opens_total",
    "Number of times a circuit breaker opened",
    ["breaker"],                      # breaker: llm | summary
)

# ── Histograms ───────────────────────────────────────────────────────────────
llm_call_duration_seconds = Histogram(
    "llm_call_duration_seconds",
    "LLM API call latency",
    ["provider"],
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60],
)

pipeline_duration_seconds = Histogram(
    "pipeline_duration_seconds",
    "Full message-to-response pipeline duration",
    buckets=[0.5, 1, 2, 5, 10, 20, 30],
)

# ── Gauges ───────────────────────────────────────────────────────────────────
active_interviews = Gauge(
    "active_interviews",
    "Number of interviews currently in progress",
)


def start_metrics_server() -> None:
    """Start Prometheus metrics HTTP server in a daemon thread."""
    try:
        start_http_server(METRICS_PORT)
        print(f"[METRICS] Prometheus metrics available at :{METRICS_PORT}/metrics")
    except OSError as e:
        print(f"[METRICS] Warning: could not start metrics server on port {METRICS_PORT}: {e}")
