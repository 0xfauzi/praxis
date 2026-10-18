"""Ugly end-to-end proof: can agent hunks be told from human hunks with nothing but
what a PostToolUse hook receives and `git diff`?

Input: the real PostToolUse body for an Edit from telltale's recorded S1 session
(Claude Code 2.1.257), which carries `originalFile`, `oldString`, `newString` and
`structuredPatch`. No text is persisted by the mechanism: an agent hunk becomes a sha256
of its changed lines, and the git diff is hashed the same way and matched.

Three scenarios in a fresh git repo, each asserted:
  S1  agent edit only                 -> every diff run is agent
  S2  agent edit + human edits elsewhere -> 1 agent run, 2 human runs
  S3  agent edit, then human alters a line INSIDE the agent's hunk
                                      -> the agent run no longer matches: ambiguous,
                                         and it is reported as such, not as human

The script is its own judge: it exits 1 if any scenario's counts differ from the
expectation written here before it ran.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path("/home/user/telltale/fixtures/sources/claude/2.1.257/S1/hooks.jsonl")


def edit_body() -> dict:
    for line in HOOKS.read_text().splitlines():
        rec = json.loads(line)
        body = rec.get("body_json") or rec.get("body") or rec
        if isinstance(body, str):
            body = json.loads(body)
        if body.get("hook_event_name") == "PostToolUse" and body.get("tool_name") == "Edit":
            return body
    raise SystemExit("no PostToolUse Edit in fixture")


def runs_from_lines(lines: list[str]) -> list[str]:
    """Split unified-diff lines into runs of consecutive +/- lines, hash each run.

    Context lines and positions are ignored on purpose: a later edit elsewhere in the
    file shifts line numbers and changes context, and must not break the match.
    """
    out: list[str] = []
    cur: list[str] = []
    for ln in lines:
        if ln[:1] in "+-" and not ln.startswith(("+++", "---")):
            cur.append(ln)
        else:
            if cur:
                out.append(hashlib.sha256("\n".join(cur).encode()).hexdigest())
                cur = []
    if cur:
        out.append(hashlib.sha256("\n".join(cur).encode()).hexdigest())
    return out


def agent_runs(structured_patch: list[dict]) -> list[str]:
    runs: list[str] = []
    for hunk in structured_patch:
        runs.extend(runs_from_lines(hunk["lines"]))
    return runs


def git_runs(repo: Path) -> list[str]:
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "--unified=0", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    # `@@` lines stay: with --unified=0 they are the only thing separating one hunk's
    # changed lines from the next, and runs_from_lines splits on any non +/- line.
    body = [ln for ln in diff.splitlines() if not ln.startswith(("diff ", "index "))]
    return runs_from_lines(body)


def attribute(agent: list[str], diff: list[str]) -> dict[str, int]:
    agent_set = set(agent)
    matched = sum(1 for r in diff if r in agent_set)
    unmatched_diff = len(diff) - matched
    # An agent run with no counterpart in the diff is one the human changed afterwards
    # (or reverted). It is reported as ambiguous, never folded into "human".
    agent_missing = sum(1 for r in agent_set if r not in set(diff))
    return {
        "diff_runs": len(diff),
        "agent": matched,
        "human": unmatched_diff - min(unmatched_diff, agent_missing),
        "ambiguous": min(unmatched_diff, agent_missing) + max(0, agent_missing - unmatched_diff),
    }


def fresh_repo(original: str) -> Path:
    repo = Path(tempfile.mkdtemp()) / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "calc.py").write_text(original)
    for args in (
        ("init", "-q"), ("config", "user.email", "t@example.invalid"),
        ("config", "user.name", "t"), ("add", "."), ("commit", "-q", "-m", "base"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


def main() -> int:
    body = edit_body()
    resp = body["tool_response"]
    original: str = resp["originalFile"]
    after_agent = original.replace(resp["oldString"], resp["newString"], 1)
    agent = agent_runs(resp["structuredPatch"])
    print(f"agent runs from structuredPatch: {len(agent)}")

    failures = 0

    def check(name: str, got: dict[str, int], want: dict[str, int]) -> None:
        nonlocal failures
        ok = all(got[k] == v for k, v in want.items())
        print(f"{name}: {got}  expected {want}  {'OK' if ok else 'FAIL'}")
        failures += 0 if ok else 1

    # S1: agent edit only.
    repo = fresh_repo(original)
    (repo / "pkg" / "calc.py").write_text(after_agent)
    check("S1 agent only", attribute(agent, git_runs(repo)), {"agent": 1, "human": 0, "ambiguous": 0})

    # S2: agent edit, then the human changes a line in `add` and appends a function.
    repo = fresh_repo(original)
    assert "    return left + right" in after_agent, "scenario assumes add() has this line"
    text = after_agent.replace("    return left + right", "    return right + left", 1)
    text += "\n\ndef human_added() -> int:\n    return 42\n"
    (repo / "pkg" / "calc.py").write_text(text)
    check("S2 agent + human elsewhere", attribute(agent, git_runs(repo)), {"agent": 1, "human": 2, "ambiguous": 0})

    # S3: the human alters a line inside the agent's hunk.
    repo = fresh_repo(original)
    text = after_agent.replace('raise ValueError("divide by zero")', 'raise ZeroDivisionError("divide by zero")', 1)
    (repo / "pkg" / "calc.py").write_text(text)
    check("S3 human inside agent hunk", attribute(agent, git_runs(repo)), {"agent": 0, "human": 0, "ambiguous": 1})

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
