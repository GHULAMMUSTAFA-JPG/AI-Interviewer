"""
STT Audio Router — ensures PulseAudio routes are correct.

Handles:
- Moving Chrome sink-inputs to VirtualSink (meeting audio)
- Verifying Chrome source-outputs are on virtual_mic_source (TTS audio)
- Periodic maintenance every 5 seconds
"""
import asyncio
from logger import push_log


async def _route_all_sink_inputs_to_virtualsink() -> int:
    """
    Move Chrome's PulseAudio sink inputs to VirtualSink.

    Skips pacat sink inputs -- those are TTS audio that must stay on
    virtual_mic so they flow: virtual_mic -> virtual_mic.monitor ->
    Chrome WebRTC mic input -> meeting participants can hear the bot.

    Only Chrome's WebRTC output (participant voices) should go to
    VirtualSink so STT can read from VirtualSink.monitor.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "list", "sink-inputs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()

        # Parse full sink-inputs output: extract id and application.name
        current_id = None
        current_app = None
        sink_inputs = []

        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Sink Input #"):
                if current_id is not None:
                    sink_inputs.append((current_id, current_app or ""))
                current_id = line.split("#")[1].strip()
                current_app = None
            elif "application.name" in line and "=" in line:
                current_app = line.split("=", 1)[1].strip().strip('"')

        if current_id is not None:
            sink_inputs.append((current_id, current_app or ""))

        moved = 0
        for sink_input_id, app_name in sink_inputs:
            # Skip pacat -- that's TTS audio playing on virtual_mic
            if "pacat" in app_name.lower():
                continue

            mv = await asyncio.create_subprocess_exec(
                "pactl", "move-sink-input", sink_input_id, "VirtualSink",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await mv.communicate()
            if mv.returncode == 0:
                moved += 1

        return moved
    except Exception as e:
        print(f"_route_all_sink_inputs error: {e}")
        return 0


async def _verify_source_outputs():
    """
    Verify Chrome's mic source-output is on virtual_mic_source.

    pactl list short source-outputs format:
        INDEX  SOURCE  APP_NAME  VOLUME  FORMAT  CHANNEL_MAP
          0      3    Chromium   65536   s16le   front-left,front-right

    parts[0] = INDEX, parts[1] = SOURCE index, parts[2] = APP_NAME
    We need Chrome on SOURCE index 2 (virtual_mic.monitor) or 3 (virtual_mic_source).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "list", "short", "source-outputs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().strip().splitlines()
        fixed = 0
        for line in lines:
            parts = line.split()
            if len(parts) >= 3:
                source_output_id = parts[0]
                source_idx = parts[1]  # FIX: was parts[2] (app name!)
                # 2 = virtual_mic.monitor, 3 = virtual_mic_source
                if source_idx not in ("2", "3"):
                    mv = await asyncio.create_subprocess_exec(
                        "pactl", "move-source-output", source_output_id, "3",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, _ = await mv.communicate()
                    fixed += 1
        return fixed
    except Exception as e:
        print(f"_verify_source_outputs error: {e}")
        return 0


async def create_audio_router():
    """
    Create periodic audio routing maintenance task.

    Runs every 5 seconds:
    1. Re-route sink-inputs (Chrome output → VirtualSink)
    2. Verify source-outputs (Chrome mic → virtual_mic_source)
    """
    async def _maintain_audio_routing():
        try:
            while True:
                await asyncio.sleep(5)
                n = await _route_all_sink_inputs_to_virtualsink()
                if n > 0:
                    msg = f"Audio routing: re-routed {n} sink-input(s) to VirtualSink"
                    print(msg); await push_log(msg)

                fixed = await _verify_source_outputs()
                if fixed > 0:
                    msg = f"Audio routing: fixed {fixed} drifted source-output(s)"
                    print(msg); await push_log(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            msg = f"Audio routing maintenance error: {e}"
            print(msg); await push_log(msg)

    return asyncio.create_task(_maintain_audio_routing())


# Export for initial setup
async def setup_audio_routing_after_admission():
    """
    Run once after Chrome is admitted to the meeting.

    1. Route Chrome output → VirtualSink
    2. Switch default source → BotMic (for STT)
    3. Verify Chrome mic source-output is on virtual_mic_source
    """
    # Step 1: Route Chrome WebRTC output → VirtualSink
    n = await _route_all_sink_inputs_to_virtualsink()
    msg = f"Audio routing: moved {n} sink input(s) to VirtualSink"
    print(msg); await push_log(msg)

    # Step 2: Switch default source → BotMic (only affects NEW streams)
    try:
        result = await asyncio.create_subprocess_exec(
            "pactl", "set-default-source", "BotMic",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await result.communicate()
        msg = f"Switched default source to BotMic (for STT only)"
        print(msg); await push_log(msg)
    except Exception as e:
        msg = f"Failed to switch to BotMic: {e}"
        print(msg); await push_log(msg)

    # Step 3: Verify Chrome's mic source-output is on virtual_mic_source
    fixed = await _verify_source_outputs()
    if fixed > 0:
        msg = f"Fixed {fixed} Chrome source-output(s) to virtual_mic_source"
        print(msg); await push_log(msg)
