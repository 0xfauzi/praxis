"""R3: what a Claude Code hook pays to deliver one PostToolUse body to telltale's receiver.

Starts the real Receiver over a real Store in a temporary home, replays the recorded
E01 S1 session once to warm it, then posts the PostToolUse bodies from that session
`ROUNDS` times each and reports wall-clock percentiles in milliseconds. Also times
the full S1 replay end to end (every record, in ingest order).
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path("/home/user/telltale")
sys.path.insert(0, str(ROOT / "src"))

from telltale.receiver import Receiver, _post, _replay_records  # noqa: E402
from telltale.store import Store  # noqa: E402

FIXTURE = ROOT / "fixtures/sources/claude/2.1.257/S1"
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 200


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main() -> None:
    home = Path(tempfile.mkdtemp())
    os.environ["HOME"] = str(home)
    os.environ["TELLTALE_HOME"] = str(home)
    store = Store(home / "telltale.db").open()
    live = Receiver(store, level=1, ctx_for_capture=None, provider="claude")
    port = live.start()

    records = _replay_records(FIXTURE)
    hook_bodies = [
        (route, body)
        for route, body in records
        if route.startswith("/hooks")
        and json.loads(body).get("hook_event_name") == "PostToolUse"
    ]
    print(f"S1 records: {len(records)}, PostToolUse hook bodies: {len(hook_bodies)}")

    t0 = time.perf_counter()
    statuses: dict[int, int] = {}
    for route, body in records:
        s = _post(port, route, body)
        statuses[s] = statuses.get(s, 0) + 1
    full = (time.perf_counter() - t0) * 1000
    print(f"full S1 replay (warm-up): {full:.1f} ms, statuses {statuses}")

    lat: list[float] = []
    for _ in range(ROUNDS):
        for route, body in hook_bodies:
            t = time.perf_counter()
            _post(port, route, body)
            lat.append((time.perf_counter() - t) * 1000)
    print(
        f"PostToolUse POST, n={len(lat)}: p50 {pct(lat, 50):.2f} ms, "
        f"p95 {pct(lat, 95):.2f} ms, p99 {pct(lat, 99):.2f} ms, "
        f"max {max(lat):.2f} ms, mean {statistics.mean(lat):.2f} ms"
    )
    live.stop()
    store.close()
    size = (home / "telltale.db").stat().st_size
    print(f"db size after {len(lat) + len(records)} posts: {size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
