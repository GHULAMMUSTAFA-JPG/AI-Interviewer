"""
Meeting Watcher — monitors interview status and handles leave/timeout.

Two stop-detection paths run concurrently:
1. Redis pub/sub (fast, ~50ms): subscribes to interview.events channel.
   UI /stop publishes meeting_status_changed {status:"abandoned"} here.
2. MongoDB poll (fallback, every 2s): catches stops that missed Redis.

Both paths call _do_leave() which stops the browser cleanly.
"""
import asyncio
import json
from datetime import datetime
from logger import push_log
from state_manager import set_bot_state


async def watch_for_leave(interview_id: str, page, db=None, mongo_connected=False) -> None:
    """
    Watch interview status and leave when it ends.

    Runs two concurrent subtasks:
    - _redis_stop_watcher: instant leave via Redis pub/sub
    - _mongo_poll_watcher: 2-second fallback polling

    First one to fire triggers _do_leave() and cancels the other.
    """
    leave_triggered = asyncio.Event()

    async def _do_leave(reason: str, current_status: str) -> None:
        """Clean shutdown: stop JS recognition, click leave, navigate away."""
        if leave_triggered.is_set():
            return  # Already leaving
        leave_triggered.set()

        msg = f"Leaving meeting (reason={reason}, status={current_status})"
        print(msg); await push_log(msg)

        # Stop the Web Speech API cleanly before leaving
        try:
            await page.evaluate("window.__stop_interview()")
            await push_log("STT stopped via __stop_interview()")
        except Exception as e:
            await push_log(f"__stop_interview() error (non-fatal): {e}")

        # Confirm final status in MongoDB
        try:
            if mongo_connected and db:
                await db.interviews.update_one(
                    {"interview_id": interview_id},
                    {"$set": {"status": current_status, "ended_at": datetime.utcnow()}}
                )
                await push_log(f"MongoDB status confirmed: {current_status}")
        except Exception as e:
            await push_log(f"MongoDB status update error: {e}")

        # Click Leave button
        try:
            for sel in ['button[aria-label="Leave call"]', 'button[aria-label*="Leave" i]']:
                btn = page.locator(sel)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    await page.wait_for_timeout(500)
                    await push_log("Left meeting via Leave button")
                    break
            await page.goto("about:blank", timeout=3000)
        except Exception as e:
            await push_log(f"Leave/navigate error (non-fatal): {e}")

    async def _redis_stop_watcher() -> None:
        """
        Fast path: subscribe to interview.events Redis pub/sub channel.
        UI /stop publishes meeting_status_changed {status:"abandoned"} here.
        Latency: ~50ms vs 2s for MongoDB poll.
        """
        try:
            from redis_client import get_redis
            redis = await get_redis()
            pubsub = redis.pubsub()
            await pubsub.subscribe("interview.events")
            msg = "Leave watcher: Redis fast-stop active"
            print(msg); await push_log(msg)

            async for message in pubsub.listen():
                if leave_triggered.is_set():
                    return
                if message["type"] != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    event_data = data.get("data", {})
                    if event_data.get("interview_id") != interview_id:
                        continue
                    status = event_data.get("status", "")
                    if status in ("abandoned", "completed"):
                        await _do_leave(f"redis_event:{data.get('event')}", status)
                        return
                except (json.JSONDecodeError, KeyError):
                    pass

        except asyncio.CancelledError:
            return
        except Exception as e:
            msg = f"Redis leave watcher unavailable: {e} — MongoDB poll is the only guard"
            print(msg); await push_log(msg)

    async def _mongo_poll_watcher() -> None:
        """
        Fallback path: poll MongoDB every 2s for status changes.
        Also handles the 2-hour hard timeout.
        """
        start_time = asyncio.get_event_loop().time()
        timeout_seconds = 7200  # 2-hour hard fallback
        last_status_check = None

        while not leave_triggered.is_set():
            await asyncio.sleep(2)

            # Hard timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout_seconds:
                msg = f"Meeting TIMEOUT ({timeout_seconds}s) — leaving"
                print(msg); await push_log(msg)
                try:
                    if mongo_connected and db:
                        await db.interviews.update_one(
                            {"interview_id": interview_id},
                            {"$set": {"status": "abandoned", "ended_at": datetime.utcnow()}}
                        )
                except Exception as e:
                    await push_log(f"Timeout status update error: {e}")
                await _do_leave("hard_timeout", "abandoned")
                return

            try:
                if not mongo_connected or db is None:
                    continue

                interview = await db.interviews.find_one(
                    {"interview_id": interview_id},
                    projection={"status": 1}
                )

                current_status = interview.get("status") if interview else None

                if current_status != last_status_check:
                    msg = f"Status check: {current_status} (was: {last_status_check})"
                    print(msg); await push_log(msg)
                    last_status_check = current_status

                if interview and current_status in ("abandoned", "completed"):
                    await _do_leave("mongo_poll", current_status)
                    return

            except asyncio.CancelledError:
                return
            except Exception as e:
                await push_log(f"Status check error: {e}")

    # Run both watchers concurrently — first to fire wins via leave_triggered Event
    redis_task = asyncio.create_task(_redis_stop_watcher())
    mongo_task = asyncio.create_task(_mongo_poll_watcher())

    try:
        # Wait until leave_triggered is set (either watcher fired)
        await leave_triggered.wait()
    finally:
        for t in (redis_task, mongo_task):
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
