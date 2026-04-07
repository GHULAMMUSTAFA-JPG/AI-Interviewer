"""
Meeting Watcher — monitors interview status and handles leave/timeout.
"""
import asyncio
from datetime import datetime
from logger import push_log
from state_manager import set_bot_state


async def watch_for_leave(interview_id: str, page, db=None, mongo_connected=False) -> None:
    """
    Background task: polls interviews.interviews every 2s.
    When status becomes 'abandoned' or 'completed', exits IMMEDIATELY.
    Also includes a 2-hour timeout as fallback.
    """
    start_time = asyncio.get_event_loop().time()
    timeout_seconds = 7200  # 2 hours fallback
    last_status_check = None

    while True:
        await asyncio.sleep(2)  # Poll every 2 seconds

        # Check timeout
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > timeout_seconds:
            msg = f"⏰ Meeting TIMEOUT ({timeout_seconds}s) — leaving"
            print(msg); await push_log(msg)

            # Update status to abandoned
            try:
                if mongo_connected and db:
                    await db.interviews.update_one(
                        {"interview_id": interview_id},
                        {"$set": {"status": "abandoned", "ended_at": datetime.utcnow()}}
                    )
                    msg = "✅ Updated status=abandoned in MongoDB (timeout)"
                    print(msg); await push_log(msg)
            except Exception as e:
                msg = f"❌ Failed to update status: {e}"
                print(msg); await push_log(msg)

            # Force leave via Playwright
            try:
                for sel in ['button[aria-label="Leave call"]', 'button[aria-label*="Leave" i]']:
                    btn = page.locator(sel)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click()
                        await page.wait_for_timeout(500)
                        msg = "✅ Left meeting via Leave button (timeout)"
                        print(msg); await push_log(msg)
                        break
                await page.goto("about:blank", timeout=3000)
            except Exception as e:
                msg = f"⚠️  Force leave error: {e}"
                print(msg); await push_log(msg)
            return

        try:
            if not mongo_connected or db is None:
                continue

            interview = await db.interviews.find_one(
                {"interview_id": interview_id},
                projection={"status": 1}
            )

            current_status = interview.get("status") if interview else None

            # Log status changes
            if current_status != last_status_check:
                msg = f"📊 Status check: {current_status} (was: {last_status_check})"
                print(msg); await push_log(msg)
                last_status_check = current_status

            if interview and current_status in ("abandoned", "completed"):
                msg = f"🚨 Interview ENDED (status={current_status}) — leaving IMMEDIATELY"
                print(msg); await push_log(msg)

                # Confirm update
                try:
                    await db.interviews.update_one(
                        {"interview_id": interview_id},
                        {"$set": {"status": current_status, "ended_at": datetime.utcnow()}}
                    )
                    msg = "✅ Confirmed status update in MongoDB"
                    print(msg); await push_log(msg)
                except Exception as e:
                    msg = f"⚠️  Status update error: {e}"
                    print(msg); await push_log(msg)

                # Force leave via Playwright IMMEDIATELY
                try:
                    for sel in ['button[aria-label="Leave call"]', 'button[aria-label*="Leave" i]']:
                        btn = page.locator(sel)
                        if await btn.count() > 0 and await btn.first.is_visible():
                            await btn.first.click()
                            await page.wait_for_timeout(500)
                            msg = "✅ Left meeting via Leave button (UI triggered)"
                            print(msg); await push_log(msg)
                            break
                    await page.goto("about:blank", timeout=3000)
                except Exception as e:
                    msg = f"⚠️  Leave error: {e}"
                    print(msg); await push_log(msg)
                return
        except Exception as e:
            msg = f"⚠️  Status check error: {e}"
            print(msg); await push_log(msg)
            pass
