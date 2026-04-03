import re

with open('docker-compose.yml', 'r', encoding='utf-8') as f:
    content = f.read()

redis_service = '''
  # ─────────────────────────────────────────────────────────────────────────────
  # Redis 7 — Real-time state management, pub/sub, caching, and queues.
  # Used for:
  #   - Bot & meeting state management (instant status updates)
  #   - Pub/sub events (real-time notifications)
  #   - Caching (reduce MongoDB load)
  #   - Task queues (TTS, STT jobs)
  #   - Heartbeat monitoring (detect dead bots)
  # ─────────────────────────────────────────────────────────────────────────────
  redis:
    image: redis:7-alpine
    container_name: redis
    command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
    ports:
      - "6379:6379"       # EXPOSED - Connect Redis CLI to localhost:6379
    volumes:
      - redis_data:/data
    networks:
      - interview-net
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 5s
    restart: unless-stopped

'''

# Insert Redis service before MongoDB
content = re.sub(
    r'(  # .*?MongoDB 7)',
    redis_service + r'\1',
    content,
    count=1,
    flags=re.DOTALL
)

# Add redis_data volume
content = re.sub(
    r'(volumes:\n  mongodb_data:)',
    r'\1\n  redis_data:        # Redis persistent data (AOF)',
    content
)

with open('docker-compose.yml', 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ Added Redis service to docker-compose.yml')
