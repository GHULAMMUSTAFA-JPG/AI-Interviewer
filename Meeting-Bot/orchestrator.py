"""
Orchestrator — watches MongoDB for new interviews and spawns one Docker container
per interview.  Each container gets its own Chrome + PulseAudio + local TTS.

Replaces the in-process asyncio.create_task approach in main.py.
Runs as the primary process of the meeting-bot service in docker-compose.

Environment variables (inherited from .env.docker):
  MEETING_BOT_IMAGE   — Docker image tag to use for bot containers
                         default: interview-meeting-bot
  MONGODB_URI         — passed through to spawned containers
  BOT_EMAIL           — passed through to spawned containers
  REDIS_URL           — passed through to spawned containers
  EDGE_TTS_VOICE      — passed through to spawned containers
  STT_LANGUAGE        — passed through to spawned containers
"""
import asyncio
import os
import signal
import docker
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

from logger import push_log
from redis_client import get_redis, close_redis

load_dotenv()

MONGO_URI     = os.getenv("MONGODB_URI", "mongodb://host.docker.internal:27017/?replicaSet=rs0")
BOT_IMAGE     = os.getenv("MEETING_BOT_IMAGE", "interview-meeting-bot")
NETWORK_NAME  = os.getenv("DOCKER_NETWORK", "interview-net")
DB_NAME       = "interviews"

# Env vars forwarded verbatim to each spawned container
_PASS_THROUGH_VARS = [
    "MONGODB_URI",
    "BOT_EMAIL",
    "REDIS_URL",
    "REDIS_HOST",
    "REDIS_PORT",
    "EDGE_TTS_VOICE",
    "STT_LANGUAGE",
    "VIRTUAL_MIC",
    "LOG_LEVEL",
]


def _build_env(interview_id: str) -> dict:
    env = {"INTERVIEW_ID": interview_id}
    for key in _PASS_THROUGH_VARS:
        val = os.getenv(key)
        if val:
            env[key] = val
    return env


async def _connect_with_retry(max_attempts: int = 10) -> AsyncIOMotorClient:
    for attempt in range(1, max_attempts + 1):
        try:
            client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
            await client.admin.command("ping")
            print(f"[ORCH] Connected to MongoDB")
            return client
        except Exception as exc:
            if attempt == max_attempts:
                raise
            wait = min(2 ** attempt, 30)
            print(f"[ORCH] MongoDB not ready (attempt {attempt}/{max_attempts}), retrying in {wait}s: {exc}")
            await asyncio.sleep(wait)


def _spawn_container(docker_client, interview_id: str) -> None:
    """Synchronous Docker SDK call — runs in a thread via asyncio.to_thread."""
    container_name = f"meeting-bot-{interview_id}"

    # Remove any stale container with the same name
    try:
        old = docker_client.containers.get(container_name)
        old.remove(force=True)
        print(f"[ORCH] Removed stale container {container_name}")
    except docker.errors.NotFound:
        pass

    container = docker_client.containers.run(
        image=BOT_IMAGE,
        name=container_name,
        environment=_build_env(interview_id),
        network=NETWORK_NAME,
        shm_size="2g",
        detach=True,
        remove=True,                # auto-remove on exit
        extra_hosts={"host.docker.internal": "host-gateway"},
    )
    print(f"[ORCH] Spawned container {container_name} (id={container.short_id})")


async def _spawn(docker_client, interview_id: str, redis_client=None) -> None:
    """Set Redis flag then spawn the container in a thread (non-blocking)."""
    # Set tts:{id}:local BEFORE spawning so shared TTS skips transcripts immediately
    if redis_client:
        try:
            await redis_client.setex(f"tts:{interview_id}:local", 7200, "1")
        except Exception as e:
            print(f"[ORCH] Redis flag warning: {e}")

    try:
        await asyncio.to_thread(_spawn_container, docker_client, interview_id)
    except Exception as exc:
        print(f"[ORCH] Failed to spawn container for {interview_id}: {exc}")
        await push_log(f"[ORCH ERROR] spawn failed for {interview_id}: {exc}")


async def main() -> None:
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except (NotImplementedError, OSError):
            signal.signal(sig, lambda _s, _f: shutdown_event.set())

    print(f"[ORCH] Starting orchestrator (image={BOT_IMAGE}, network={NETWORK_NAME})")
    await push_log(f"[ORCH] Orchestrator started, watching for new interviews")

    # Connect Docker SDK
    try:
        docker_client = await asyncio.to_thread(docker.from_env)
        print("[ORCH] Docker SDK connected")
    except Exception as exc:
        print(f"[ORCH] Docker SDK failed: {exc} — is /var/run/docker.sock mounted?")
        return

    # Connect Redis (for setting tts:local flag)
    redis_client = None
    try:
        redis_client = await get_redis()
        print("[ORCH] Redis connected")
    except Exception as e:
        print(f"[ORCH] Redis not available (continuing without flag): {e}")

    mongo_client = await _connect_with_retry()
    db = mongo_client[DB_NAME]

    # Tracks active interview_ids to prevent duplicate containers
    _active: set[str] = set()

    retry_delay = 2.0
    while not shutdown_event.is_set():
        try:
            async with db["interviews"].watch(
                [{"$match": {"operationType": "insert"}}],
                full_document="updateLookup",
            ) as stream:
                retry_delay = 2.0
                print("[ORCH] Change stream open — watching for new interviews")

                async for change in stream:
                    if shutdown_event.is_set():
                        break

                    doc = change.get("fullDocument") or {}
                    if doc.get("status") != "in_progress":
                        continue

                    interview_id = str(doc.get("interview_id", doc.get("_id", "unknown")))
                    if interview_id in _active:
                        continue

                    _active.add(interview_id)
                    print(f"[ORCH] New interview {interview_id} — spawning container")
                    await push_log(f"[ORCH] Spawning bot container for {interview_id}")

                    task = asyncio.create_task(_spawn_and_track(
                        docker_client, redis_client, interview_id, _active
                    ))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            if shutdown_event.is_set():
                break
            print(f"[ORCH] Change stream error (retry in {retry_delay}s): {exc}")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)

    await close_redis()
    print("[ORCH] Orchestrator stopped")


async def _spawn_and_track(docker_client, redis_client, interview_id: str, active: set) -> None:
    try:
        await _spawn(docker_client, interview_id, redis_client)
    finally:
        # Don't remove from active — we want duplicate prevention to persist
        # for the lifetime of the orchestrator process.
        pass


if __name__ == "__main__":
    asyncio.run(main())
