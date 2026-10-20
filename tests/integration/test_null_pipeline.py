"""E12's runner, short: T = 6 tasks, R = 3 runs through the binary (experiments/E12/
null_pipeline.py drives `praxis.cli.main` in a fresh interpreter per command, with
the clock read from E12_NOW). This checks that the pipeline completes and that the
report prints every p. Three runs cannot test uniformity; the 200-run experiment
does that (docs/experiments/E12.md).

The runs use no skips and no new imports, so every exposed item gets a usable first
probe. Which tests can print a number then depends only on the arms, and the expected
values come from an oracle written here from the rule in task.py's docstring
(sha256 of json.dumps([seed, repo_id, ordinal]), treatment below 0.5). The repo_id
hashes the run's temporary path, so it is read from the runner's output and the arms are
computed from it before the p values are checked:

- specific, near, untargeted: a number exactly when both arms hold a task;
- pipeline placebo: always a number (6 drawn-not-shown items and the 2 never-drawn
  items of the first evening's probe);
- f1_shuffle: a number exactly when 2 or more tasks are treatment tasks;
- general effect: null, no repository in the test, and one left out as
  fewer_than_3_weeks (the never-drawn first probes fall in ISO weeks 2027-W09 and W10).

Processes per run: 1 `set components`, 2 days of 1 SessionStart and 3 tasks of 5
commands each, 10 evening probe sessions (2 task days and 8 more), 1 report: 44.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from praxis.task import SEED

pytestmark = pytest.mark.integration

RUNNER = (
    Path(__file__).resolve().parents[2] / "experiments" / "E12" / "null_pipeline.py"
)
TASKS, RUNS = 6, 3
CONTRASTS = ("specific", "near", "untargeted")
TESTS = (*CONTRASTS, "pipeline_placebo", "f1_shuffle")
GENERAL = {"run": True, "p": None, "repositories": 0,
           "left_out": {"fewer_than_3_weeks": 1}}  # fmt: skip


def oracle_treated(repo_id: str) -> int:
    treated = 0
    for ordinal in range(1, TASKS + 1):
        key = hashlib.sha256(json.dumps([SEED, repo_id, ordinal]).encode())
        treated += random.Random(int(key.hexdigest(), 16)).random() < 0.5
    return treated


def test_a_short_null_run_completes_and_prints_every_p(tmp_path: Path) -> None:
    out = tmp_path / "e12.json"
    argv = [sys.executable, str(RUNNER), "--runs", str(RUNS), "--tasks", str(TASKS),
            "--skip-rate", "0", "--edge-rate", "0", "--workdir", str(tmp_path),
            "--out", str(out)]  # fmt: skip
    started = time.monotonic()
    done = subprocess.run(
        argv, capture_output=True, text=True, timeout=110, check=False
    )
    seconds = time.monotonic() - started
    assert done.returncode == 0, done.stderr
    assert seconds < 60, seconds
    result = json.loads(out.read_text())
    assert result["summary"]["errors"] == []
    assert len(result["runs"]) == RUNS
    for run in result["runs"]:
        treated = oracle_treated(run["repo_id"])
        assert run["arms"].get("treatment", 0) == treated, run["repo_id"]
        assert sum(run["arms"].values()) == TASKS
        assert run["processes"] == 44
        assert (run["skips"], run["stored_skips"]) == (0, 0)
        assert run["canary_absent"] is True
        assert sorted(run["tests"]) == sorted(TESTS)
        computable = dict.fromkeys(CONTRASTS, 0 < treated < TASKS)
        computable |= {"pipeline_placebo": True, "f1_shuffle": treated >= 2}
        for name, expect in computable.items():
            found = run["tests"][name]
            assert (found is not None) == expect, (name, treated, found)
            if found is not None:
                assert 0 < found["p"] <= 1, (name, found)
                assert found["allocations"] >= 1, (name, found)
        assert run["general"] == GENERAL
    # The summary carries a verdict per test; at R = 3 it is not a uniformity result.
    verdicts = result["summary"]["verdicts"]
    assert [v["test"] for v in verdicts] == list(TESTS)
