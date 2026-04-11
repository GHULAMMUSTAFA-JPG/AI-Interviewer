"""
STT Audio Router -- ensures PulseAudio routes are correct.

Target audio graph
------------------
  Meeting participants speak
    -> WebRTC -> Chrome sink-input -> [router moves to] VirtualSink
    -> VirtualSink.monitor -> BotMic (loopback source)
    -> Web Speech API reads from BotMic -> candidate transcription OK

  Bot TTS (pacat) writes to virtual_mic null-sink
    -> virtual_mic.monitor -> virtual_mic_source (remap-source)
    -> Chrome WebRTC source-output -> Google Meet -> participants hear bot OK

Critical invariant
------------------
  Chrome has two types of source-outputs in PulseAudio:
    1. WebRTC mic source-output  -- must be on virtual_mic_source
       (sends bot TTS audio to Google Meet participants)
    2. Web Speech API source-output -- must stay on BotMic
       (reads candidate voices from VirtualSink.monitor)

  _verify_source_outputs() preserves this by treating BotMic source-outputs
  as "already correct" -- never moving them to virtual_mic_source.
  Only source-outputs that have drifted to an unexpected source are moved.

Handles:
- Moving Chrome sink-inputs to VirtualSink (meeting audio for STT)
- Verifying Chrome WebRTC source-outputs are on virtual_mic_source
- NOT disturbing STT source-outputs that are correctly on BotMic
- Periodic maintenance every 2 seconds (was 5 s -- faster drift recovery)
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


async def _get_source_index(name: str) -> str | None:
    """Resolve a PulseAudio source name to its current numeric index."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "list", "short", "sources",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode().strip().splitlines():
            parts = line.split()
            # format: INDEX  NAME  MODULE  SAMPLE_SPEC  STATE
            if len(parts) >= 2 and parts[1] == name:
                return parts[0]
    except Exception:
        pass
    print(f"PulseAudio source '{name}' not found — audio routing may be broken")
    return None


async def _get_source_outputs_verbose():
    """
    Parse 'pactl list source-outputs' (verbose) to get detailed info.

    Example line: "    Source: 3 / BotMic"
    Returns list of dicts with source_index="3", source_name="BotMic"
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "list", "source-outputs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()

        source_outputs = []
        current = {}

        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Source Output #"):
                if current:
                    source_outputs.append(current)
                current = {"index": line.split("#")[1].strip()}
            elif "Source: " in line and "=" not in line:
                # Format: "Source: 3 / BotMic"  or  "Source: 0 / virtual_mic_source"
                slash_parts = line.split("/", 1)
                if len(slash_parts) == 2:
                    # Left side: "Source: 3"  →  extract index number
                    left = slash_parts[0].strip()  # "Source: 3"
                    tokens = left.split()
                    if len(tokens) >= 2:
                        current["source_index"] = tokens[-1]  # "3"
                    # Right side: "BotMic" or "virtual_mic_source"
                    current["source_name"] = slash_parts[1].strip()
            elif "application.name" in line:
                current["application_name"] = line.split("=", 1)[1].strip().strip('"')
            elif "media.name" in line:
                current["media_name"] = line.split("=", 1)[1].strip().strip('"')
            elif "driver:" in line:
                current["driver"] = line.split(":", 1)[1].strip()

        if current:
            source_outputs.append(current)

        return source_outputs
    except Exception as e:
        print(f"_get_source_outputs_verbose error: {e}")
        return []


async def _verify_source_outputs():
    """
    Verify Chrome source-outputs are on the correct PulseAudio sources.

    Two-source invariant
    ---------------------
    - virtual_mic_source: Chrome WebRTC mic source-output
      (sends bot TTS audio to Google Meet participants). Must stay here.
    - BotMic: Chrome Web Speech API source-output
      (reads candidate voices from VirtualSink.monitor). Must NOT be on virtual_mic_source.

    Uses verbose pactl output to enumerate all Chrome source-outputs,
    then assigns them by creation order: first = WebRTC, second = STT.
    """
    try:
        # Get all source-outputs with full details
        source_outputs = await _get_source_outputs_verbose()
        if not source_outputs:
            return 0

        # Filter to Chrome source-outputs only
        chrome_sos = [
            so for so in source_outputs
            if "chrome" in so.get("application_name", "").lower()
            or "google" in so.get("application_name", "").lower()
        ]
        if not chrome_sos:
            return 0

        # Resolve source indices by name
        mic_idx    = await _get_source_index("virtual_mic_source")
        botmic_idx = await _get_source_index("BotMic")

        # Identify by media.name — reliable across reconnects.
        # Chrome labels Web Speech API streams as "VoiceCapture" or containing "speech".
        # WebRTC mic streams use "webrtc" or "WebRTC" in media.name.
        # Fall back to creation-order only if media.name is missing on both.
        def _classify(so: dict) -> str:
            """Return 'stt', 'webrtc', or 'unknown' based on media.name."""
            media = so.get("media_name", "").lower()
            if "speech" in media or "voice" in media or "capture" in media:
                return "stt"
            if "webrtc" in media or "web rtc" in media:
                return "webrtc"
            return "unknown"

        for so in chrome_sos:
            so["_role"] = _classify(so)

        # If media.name classification fails for all, fall back to index order
        roles_known = any(so["_role"] != "unknown" for so in chrome_sos)
        if not roles_known:
            chrome_sos.sort(key=lambda so: int(so.get("index", "0")))
            for i, so in enumerate(chrome_sos):
                so["_role"] = "webrtc" if i == 0 else "stt"

        fixed = 0
        for so in chrome_sos:
            so_idx = so.get("index")
            src_name = so.get("source_name", "")
            src_idx = so.get("source_index", "")
            role = so["_role"]

            if role == "stt":
                target_source = "BotMic"
                target_idx = botmic_idx
                label = "STT"
            else:
                # webrtc or unknown — default to virtual_mic_source
                target_source = "virtual_mic_source"
                target_idx = mic_idx
                label = "WebRTC"

            # Check if already on correct source
            if src_name == target_source or src_idx == target_idx:
                continue

            # Move to correct source
            mv = await asyncio.create_subprocess_exec(
                "pactl", "move-source-output", so_idx, target_source,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, _ = await mv.communicate()
            if mv.returncode == 0:
                fixed += 1
                msg = f"Moved {label} source-output #{so_idx} from '{src_name}' to {target_source}"
                print(msg); await push_log(msg)

        return fixed
    except Exception as e:
        print(f"_verify_source_outputs error: {e}")
        return 0


async def create_audio_router():
    """
    Create periodic audio routing maintenance task.

    Schedule:
      - First 30 s: check every 1 s (burst mode -- Chrome opens new streams
        right after joining, we need to catch them fast).
      - After 30 s: check every 2 s (steady state -- was 5 s).

    Each cycle:
      1. Re-route sink-inputs  (Chrome audio output -> VirtualSink for STT)
      2. Verify source-outputs (Chrome WebRTC mic -> virtual_mic_source for TTS)
         Leaves BotMic source-outputs alone (those are Web Speech API streams).
    """
    async def _maintain_audio_routing():
        import time as _time
        start = _time.monotonic()
        try:
            while True:
                elapsed = _time.monotonic() - start
                interval = 1.0 if elapsed < 30 else 2.0
                await asyncio.sleep(interval)

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
async def setup_audio_routing_after_admission(page=None):
    """
    Run once after Chrome is admitted to the meeting.

    1. Route Chrome output -> VirtualSink
    2. Switch default source -> BotMic (for STT)
       (Web Speech API recognition created AFTER this will pick up BotMic)
    3. Verify Chrome mic source-output is on virtual_mic_source
    """
    # Step 1: Route Chrome WebRTC output -> VirtualSink
    n = await _route_all_sink_inputs_to_virtualsink()
    msg = f"Audio routing: moved {n} sink input(s) to VirtualSink"
    print(msg); await push_log(msg)

    # Step 2: Switch default source -> BotMic (only affects NEW streams)
    # The Web Speech API recognition is injected AFTER this function,
    # so it will grab BotMic as its source automatically.
    try:
        result = await asyncio.create_subprocess_exec(
            "pactl", "set-default-source", "BotMic",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await result.communicate()
        msg = "Switched default source to BotMic (for STT only)"
        print(msg); await push_log(msg)
    except Exception as e:
        msg = f"Failed to switch to BotMic: {e}"
        print(msg); await push_log(msg)

    # Step 3: Verify Chrome's mic source-output is on virtual_mic_source
    fixed = await _verify_source_outputs()
    if fixed > 0:
        msg = f"Fixed {fixed} Chrome source-output(s) to virtual_mic_source"
        print(msg); await push_log(msg)
