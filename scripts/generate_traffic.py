import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description="Generate 24 synthetic vacation-agent requests.")
parser.add_argument("--version", required=True, help="Deployed agent version to invoke.")
args = parser.parse_args()
environment = os.environ.copy()
scenarios = [
    ("Kyoto", 1600, "temples, vegetarian food and easy walks", 4),
    ("Lisbon", 1200, "food markets and history", 3),
    ("Vancouver", 1800, "nature and public transit", 4),
    ("Reykjavik", 2200, "geothermal pools and accessible sightseeing", 3),
    ("Barcelona", 1400, "architecture and beaches", 4),
    ("Singapore", 1500, "gardens and vegetarian hawker food", 3),
    ("Edinburgh", 1100, "museums and easy walks", 3),
    ("Rome", 1700, "history and inexpensive local food", 4),
    ("Copenhagen", 1900, "design and cycling", 3),
    ("Seoul", 1300, "markets and palaces", 4),
    ("San Diego", 2000, "beaches and family-friendly activities", 4),
    ("Melbourne", 1600, "cafes, art and public transit", 3),
]
results = []
session = None
consecutive_failures = 0


def invoke(prompt, conversation=None):
    global session, consecutive_failures
    args = ["azd", "ai", "agent", "invoke"]
    if session:
        args += ["--session-id", session]
    else:
        args += ["--version", version, "--new-session"]
    args += ["--conversation-id", conversation] if conversation else ["--new-conversation"]
    result = subprocess.run(
        args + [prompt], cwd=root, env=environment, text=True,
        capture_output=True, timeout=180, encoding="utf-8", errors="replace",
    )
    output = result.stdout + result.stderr
    metadata = {}
    for key, pattern in {
        "session": r"Session:\s+([A-Za-z0-9_-]{20,})",
        "conversation": r"Conversation:\s+(conv_[A-Za-z0-9]+)",
        "trace": r"Trace ID:\s+([a-f0-9]+)",
        "response": r"Response:\s+(caresp_[A-Za-z0-9]+)",
    }.items():
        match = re.search(pattern, output)
        if match:
            metadata[key] = match.group(1)
    if metadata.get("session"):
        session = metadata["session"]
    metadata["success"] = result.returncode == 0
    if result.returncode:
        metadata["error"] = next(
            (line for line in output.splitlines() if line.startswith("ERROR:")),
            f"azd exited {result.returncode}",
        )
        consecutive_failures += 1
    else:
        consecutive_failures = 0
    results.append(metadata)
    (root / "traffic-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Request {len(results)}/24: {'completed' if metadata['success'] else 'FAILED'}; trace={metadata.get('trace')}", flush=True)
    if consecutive_failures >= 3:
        raise RuntimeError("Three consecutive requests failed; stopping demo traffic.")
    time.sleep(2)
    return metadata


version = args.version
for destination, budget, interests, days in scenarios:
    first = invoke(
        f"In under 100 words, plan a {days}-day vacation to {destination} for two adults. "
        f"Budget USD {budget}, excluding flights. Interests: {interests}. "
        "Give one short line per day, an estimated total, and one travel caveat."
    )
    if first["success"] and first.get("conversation"):
        invoke(
            "In under 60 words, remind me of my destination and original budget, "
            "then revise one day for rainy weather while staying within that budget.",
            first["conversation"],
        )
print(json.dumps({"requests": len(results),
                  "completed": sum(r["success"] for r in results),
                  "failed": sum(not r["success"] for r in results),
                  "session": session}, indent=2), flush=True)
if any(not result["success"] for result in results):
    raise SystemExit(1)
