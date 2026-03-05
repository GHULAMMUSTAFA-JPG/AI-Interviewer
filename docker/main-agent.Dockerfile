# -------------------------------------------------------
# AI Interview Agent — Docker image
# -------------------------------------------------------
FROM python:3.13-slim

WORKDIR /app

# Install dependencies in a dedicated layer so rebuilds are fast
# when only source code changes (not requirements).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/

# Run as a non-root user
RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app
USER appuser

# Unbuffered output so `docker logs` sees every line immediately
ENV PYTHONUNBUFFERED=1

# Disable file logging inside Docker — stdout/stderr are captured
# by the Docker daemon and forwarded to whatever logging driver is configured.
ENV LOG_FILE=

CMD ["python", "src/main.py"]
