"""Measure tools/call round trips against a running mcp-home, the way Alexa+ would call it.

The Alexa+ MCP quick start asks for "a round-trip query response latency of less than 500 ms". This script sends
real JSON-RPC over Streamable HTTP (initialize, then N calls of each read-only tool) and prints p50, p95 and max
per tool, plus PASS or FAIL against 500 ms at p95.

    python scripts/latency.py http://localhost:8000/mcp <household token> [calls per tool, default 20]

Run it from where the client would be: from your laptop it includes your network path to the server.
"""

from __future__ import annotations

import statistics
import sys
import time

import httpx

TOOLS = {"weekly_briefing": {}, "get_grocery_list": {}, "upcoming_deadlines": {"days": 14},
         "whose_turn": {"chore": "trash"}, "pantry_expiring": {"days": 3}, "what_can_you_do": {}}
BUDGET_MS = 500


def main(url: str, token: str, n: int = 20) -> int:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}
    with httpx.Client(headers=headers, timeout=10) as client:
        init = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "latency", "version": "1"}}
        r = client.post(url, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": init})
        r.raise_for_status()
        print(f"server {r.json()['result']['serverInfo']['name']}, protocol {r.json()['result']['protocolVersion']}")
        worst = 0.0
        print(f"{'tool':<20} {'p50 ms':>7} {'p95 ms':>7} {'max ms':>7}")
        for i, (name, args) in enumerate(TOOLS.items(), 1):
            times = []
            for k in range(n):
                body = {"jsonrpc": "2.0", "id": i * 1000 + k, "method": "tools/call",
                        "params": {"name": name, "arguments": args}}
                t0 = time.perf_counter()
                resp = client.post(url, json=body)
                times.append((time.perf_counter() - t0) * 1000)
                resp.raise_for_status()
                if resp.json().get("result", {}).get("isError"):
                    print(f"{name}: tool error {resp.json()['result']['content'][0]['text']}")
                    return 2
            p95 = statistics.quantiles(times, n=20)[-1] if len(times) >= 2 else times[0]
            worst = max(worst, p95)
            print(f"{name:<20} {statistics.median(times):7.1f} {p95:7.1f} {max(times):7.1f}")
    verdict = "PASS" if worst < BUDGET_MS else "FAIL"
    print(f"{verdict}: worst p95 {worst:.1f} ms (budget {BUDGET_MS} ms), {n} calls per tool")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 20))
