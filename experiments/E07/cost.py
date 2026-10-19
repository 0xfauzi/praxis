"""E07: what does the instrument cost the owner per day, in seconds, from the ledger's
own timestamps?

Acceptance rule, written before the run (2026-10-19). The kickoff's budget, V9 in
docs/preregistration.md 1.7: the daily median of `praxis start task` plus `praxis
test` time is at most ten minutes. Per UTC day with any task or probe row:

  task seconds    a treatment task's last forecast ts minus its registration ts (the
                  forecast rows are written once, after the last answer); a control task
                  or a treatment task with no forecast row has no stored end, so its
                  seconds are unknown and that day's total is a lower bound;
  probe seconds   the sum of probe_responses.seconds (question to accepted answer).

V9's time limb passes iff the median over days is at most 600 s with every lower-bound
day counted as above 600 s: an unknown never counts toward a pass. Also printed: the
seconds per task and per probe item (Q2), the forecast and probe skip shares, treatment
tasks with no forecast row and `abandoned` flags, and the rows per drift, decision,
knockout and Pathfinder administration, whose seconds the ledger does not hold.

This cannot run on the owner's data until the pilot exists. `--selfcheck` plants three
days through store.py with hand-worked answers: task seconds [90, 180], day totals 155
(exact), 280 and 30 (lower bounds), median 155, worst-case verdict fail, forecast skip
share 0.1, probe skip share 0.2, one treatment task without forecasts.

Usage: uv run python experiments/E07/cost.py [--home DIR] | --selfcheck
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from praxis.store import Evidence, Store, home

Clock = tuple[int, int, int] | None

BUDGET_S = 600.0
OTHER = ("drift_responses", "decision_responses", "knockouts", "pathfinder_runs")


def when(ts: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(ts)
    if stamp.tzinfo is None:
        raise ValueError("a ledger timestamp without a zone is refused")
    return stamp


def day(ts: str) -> str:
    return when(ts).astimezone(dt.UTC).date().isoformat()


def cost(ledger: Store) -> dict[str, object]:
    db = ledger.db
    totals: dict[str, float] = defaultdict(float)
    bounded: set[str] = set()
    task_s: list[float] = []
    no_forecast = asked = blank = 0
    for t in db.execute("SELECT task_id, ts, arm FROM tasks ORDER BY ts"):
        rows = ledger.forecasts_of(t["task_id"])
        asked, blank = asked + len(rows), blank + sum(r["p"] is None for r in rows)
        if t["arm"] == "treatment" and rows:
            secs = (max(when(r["ts"]) for r in rows) - when(t["ts"])).total_seconds()
            task_s.append(secs)
            totals[day(t["ts"])] += secs
            continue
        no_forecast += t["arm"] == "treatment"
        bounded.add(day(t["ts"]))
        totals[day(t["ts"])] += 0.0
    probes = db.execute("SELECT ts, seconds, skipped FROM probe_responses").fetchall()
    for r in probes:
        totals[day(r["ts"])] += r["seconds"]
    worst = [math.inf if d in bounded else s for d, s in totals.items()]
    median = statistics.median(totals.values()) if totals else None
    return {
        "days": dict(sorted(totals.items())),
        "lower_bound_days": sorted(bounded),
        "median_day_s": median,
        "v9_time_pass": bool(worst) and statistics.median(worst) <= BUDGET_S,
        "task_s": task_s,
        "probe_item_s": [r["seconds"] for r in probes],
        "forecast_skip_share": blank / asked if asked else None,
        "probe_skip_share": (
            sum(r["skipped"] for r in probes) / len(probes) if probes else None
        ),
        "treatment_without_forecast": no_forecast,
        "abandoned_flags": db.execute(
            "SELECT COUNT(*) FROM flags WHERE code = 'abandoned'"
        ).fetchone()[0],
        "rows_without_seconds": {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in OTHER
        },
    }


def stamp(d: int, hour: int, minute: int = 0, second: int = 0) -> str:
    return dt.datetime(2026, 2, d, hour, minute, second, tzinfo=dt.UTC).isoformat()


def plant(root: Path) -> Store:
    import subprocess

    from praxis.components import derive, to_json

    repo, rid = root / "repo", "fedcba9876543210"
    (repo / "pkg").mkdir(parents=True)
    for i in range(1, 6):
        (repo / "pkg" / f"m{i}.py").write_text("X = 1\n")
    for cmd in (["init", "-q"], ["add", "."]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    ledger = Store(root / "home" / "ledger.db").open()
    version = ledger.component_map_put(to_json(derive(repo, rid)), stamp(1, 9))
    ledger.component_map_freeze(rid, version, stamp(1, 9))
    ev = Evidence("full", ["selfcheck"], "selfcheck-v1", [], [])
    items = ledger.probe_items_put(
        rows=[("D1", "m1", f"m{i}", False, 0, None, None) for i in range(2, 6)]
        + [("D1", "m2", "m3", False, 0, None, None)],
        ev=ev,
        repo_id=rid,
        map_version=version,
        truth_ts=stamp(1, 9),
        extractor_version="x1-v1",
    )
    Answer = tuple[float, float | None]
    plan: list[tuple[int, str, list[str], list[float | None], Clock, list[Answer]]]
    plan = [
        (
            1,
            "treatment",
            items[:3],
            [0.7, None, 0.3, 0.5, 0.9],
            (10, 1, 30),
            [(20, 0.7), (40, 0.3), (5, None)],
        ),
        (2, "treatment", items[3:4], [0.1] * 5, (10, 3, 0), [(100, 0.5)]),
        (2, "control", [], [], None, []),
        (3, "treatment", items[4:], [], None, [(30, 0.5)]),
    ]
    for n, (d, arm, mine, ps, done, answers) in enumerate(plan, start=1):
        tid, start = f"{n:016x}", stamp(d, 10 if arm == "treatment" else 12)
        ledger.task_register(
            region=[],
            bound=mine,
            related=[],
            drawn=[],
            task_id=tid,
            repo_id=rid,
            map_version=version,
            ordinal=n,
            ts=start,
            arm=arm,
            seed_hash=f"{n:064x}",
            stratum="uniform",
        )
        if done is not None:
            rows = [(f"m{i + 1}", p, "forecast", None) for i, p in enumerate(ps)]
            ledger.forecast(tid, rows, stamp(d, *done), ev=ev)
        for item, (secs, p) in zip(mine, answers, strict=True):
            ledger.probe_response(
                ev=ev,
                item_id=item,
                task_id=tid,
                ts=stamp(d, 15),
                p=p,
                seconds=secs,
                lag_days=7.0,
                deadline_met=True,
                truth_at_probe=False,
                truth_changed=False,
                head_sha=None,
                tree_hash=None,
                dirty=None,
                extractor_version="x1-v1",
            )
    return ledger


def selfcheck() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = plant(Path(tmp))
        got = cost(ledger)
        ledger.close()
    want = {
        "task_s": [90.0, 180.0],
        "median_day_s": 155.0,
        "v9_time_pass": False,
        "days": {"2026-02-01": 155.0, "2026-02-02": 280.0, "2026-02-03": 30.0},
        "lower_bound_days": ["2026-02-02", "2026-02-03"],
        "forecast_skip_share": 0.1,
        "probe_skip_share": 0.2,
        "treatment_without_forecast": 1,
    }
    bad = [k for k, v in want.items() if got[k] != v]
    for k, v in want.items():
        print(f"{'FAIL' if k in bad else 'ok  '} {k}: got {got[k]}, want {v}")
    print("PASS" if not bad else "FAIL")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", type=Path, default=home())
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    path = args.home / "ledger.db"
    if not path.is_file():
        print("no ledger at this $PRAXIS_HOME: E07 waits for the owner's pilot")
        return 0
    ledger = Store(path).open()
    try:
        for key, value in cost(ledger).items():
            print(f"{key}: {value}")
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
