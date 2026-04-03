# ══════════════════════════════════════════════════════════════════════
# Cleanup Service Container — Interview Watchdog Supervisor
# ══════════════════════════════════════════════════════════════════════

FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY redis_client.py .

# Create non-root user for security
RUN adduser --disabled-password --gecos "" appuser && \
    chown -R appuser:appuser /app

USER appuser

# Run the cleanup service
CMD ["python", "main.py"]
