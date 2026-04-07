"""
Media Controls — microphone and camera management in Google Meet.
"""
from logger import push_log


async def ensure_mic_on(page) -> None:
    """Ensure Google Meet's microphone is UNMUTED."""
    mic_off_selectors = [
        'button[aria-label="Turn on microphone"]',
        'button[aria-label="Unmute microphone"]',
    ]
    for attempt in range(4):
        for sel in mic_off_selectors:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    await page.wait_for_timeout(500)
                    msg = f"✅ Microphone unmuted (attempt {attempt+1})"
                    print(msg); await push_log(msg)
                    return
            except Exception:
                pass
        await page.wait_for_timeout(500)
    msg = "ℹ️  Microphone already on"
    print(msg); await push_log(msg)


async def disable_camera(page) -> None:
    """Turn off the camera."""
    cam_selectors = [
        'button[aria-label="Turn off camera"]',
        'button[aria-label*="camera" i][aria-pressed="false"]',
    ]
    for attempt in range(5):
        for sel in cam_selectors:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    await page.wait_for_timeout(400)
                    msg = f"✅ Camera disabled (attempt {attempt + 1})"
                    print(msg); await push_log(msg)
                    return
            except Exception:
                pass
        await page.wait_for_timeout(500)
    msg = "ℹ️  Camera already off"
    print(msg); await push_log(msg)
