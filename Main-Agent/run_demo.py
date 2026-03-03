"""
Live interview demo — starts agent, sends 4 candidate messages, shows responses with latency.
Run with: uv run python run_demo.py
"""
import asyncio
import subprocess
import sys
import time
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

CANDIDATE_MESSAGES = [
    "Hello! I am excited to interview for this position.",
    "I have 7 years of Python development experience.",
    "I worked at TechCorp as a Senior Backend Engineer.",
    "I led a team and scaled our system to handle 10 million requests per day.",
]


async def run_test():
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    db = client["interviews"]
    interview = await db.interviews.find_one({"status": "in_progress"})
    if not interview:
        print("ERROR: No in_progress interview. Run setup_db.py first.")
        return

    iid = interview["_id"]
    print(f"Interview : {iid}")
    print(f"Candidate : {(await db.candidates.find_one({}))['name']}")
    print(f"Phase     : {interview['phase']} | Turns: {interview['turn_count']}")
    print()

    # Track the last agent message to detect new ones
    last_agent_text = ""
    last_msg = await db.transcripts.find({"interview_id": iid}).sort("timestamp", -1).limit(1).to_list(1)
    if last_msg and last_msg[0]["speaker"] == "agent":
        last_agent_text = last_msg[0]["text"]

    for i, text in enumerate(CANDIDATE_MESSAGES, 1):
        t0 = time.perf_counter()
        print(f"[{i}/{len(CANDIDATE_MESSAGES)}] CANDIDATE: {text}")

        await db.transcripts.insert_one({
            "interview_id": iid,
            "speaker": "candidate",
            "text": text,
            "timestamp": datetime.now(timezone.utc)
        })

        # Poll until a new agent response appears (max 20s)
        agent_text = None
        for _ in range(40):
            await asyncio.sleep(0.5)
            latest = await db.transcripts.find(
                {"interview_id": iid}
            ).sort("timestamp", -1).limit(1).to_list(1)
            if latest and latest[0]["speaker"] == "agent" and latest[0]["text"] != last_agent_text:
                agent_text = latest[0]["text"]
                last_agent_text = agent_text
                break

        elapsed_ms = round((time.perf_counter() - t0) * 1000)
        if agent_text:
            print(f"           AGENT   : {agent_text}")
            print(f"           Latency : {elapsed_ms}ms")
        else:
            print(f"           AGENT   : [no response in 20s — check agent_output.log]")

        interview = await db.interviews.find_one({"_id": iid})
        print(f"           Phase={interview['phase']} | Turn={interview['turn_count']}")
        print()
        await asyncio.sleep(1)

    client.close()


if __name__ == "__main__":
    # Start exactly one agent process
    # -u = unbuffered stdout/stderr so log file is written immediately
    proc = subprocess.Popen(
        [sys.executable, "-u", "src/main.py"],
        stdout=open("agent_output.log", "w"),
        stderr=subprocess.STDOUT
    )
    print(f"Agent PID {proc.pid} starting...")
    time.sleep(3)

    log = open("agent_output.log").read()
    if "AI Interview Agent started" in log:
        print("Agent ready.")
        print("=" * 60)
        print()
        asyncio.run(run_test())
    else:
        print("Agent failed to start:")
        print(log)

    proc.terminate()
    proc.wait()
    print("Agent stopped.")
