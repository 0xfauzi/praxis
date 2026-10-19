"""E03b: does diffing against the session's base commit make an agent's commit move no
line, where diffing against HEAD made it read as the agent deleting its own work?

Acceptance rule, written before the run (2026-10-19). Four scenarios, each a fresh
repository holding `pkg/calc.py` = "def add(a, b):\\n    return a + b\\n" in one commit,
each driven through `praxis hook` one process per event with the Claude Code 2.1.303
payloads recorded in fixtures/claude/2.1.303 (session id, cwd and tool_use_id replaced):

  commit   Edit calc.py (a + b -> b + a), Write pkg/extra.py ("X = 1"), then a Bash call
           that runs `git add -A && git commit -qm step`, then Stop.
  sed      one Bash call that runs `sed -i` on calc.py and `git commit -qam step`, then
           Stop.
  pull     Edit calc.py, then the owner commits a new file between tool calls (no hook
           sees it), then Stop.
  checkout the owner checks out the parent of the session's base commit, then Stop.

The installed binary (base-sha diffing, design 5.1) passes iff all of:
  commit   the commit call writes 0 agent_deltas rows; the session's agent deltas are
           exactly three +1 rows; at Stop calc.py has agent 2 and extra.py agent 1,
           human 0 on both, coverage full.
  sed      the Bash call writes two +1 rows on calc.py; at Stop calc.py has agent 2.
  pull     every attribution row at Stop carries `head_moved_outside_tool` and coverage
           partial; calc.py still has agent 2.
  checkout the Stop's hook_events row is `unobservable` with note `head_not_descendant`.

The same scenarios are run against the recorder at praxis commit e9b5e3a (HEAD diffing),
from a scratch clone, as the before condition. Predicted before the run: its commit
call writes three -1 rows and the Stop attributes nothing to the agent; its sed call
writes no row at all, so the edit is lost.

Usage: uv run python experiments/E03b/base_sha.py [BEFORE_COMMIT]
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from praxis.extract import scratch_clone

PRAXIS = Path(__file__).resolve().parents[2]
FIXTURES = PRAXIS / "fixtures" / "claude" / "2.1.303"
CALC = "def add(a, b):\n    return a + b\n"
MAIN = "import sys; from praxis.cli import main; sys.exit(main())"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def fresh(root: Path, name: str) -> Path:
    repo = root / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "calc.py").write_text(CALC)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "e03b@example.invalid")
    git(repo, "config", "user.name", "e03b")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    return repo


class Session:
    def __init__(self, cmd: list[str], env: dict[str, str], repo: Path) -> None:
        self.cmd, self.env, self.repo = cmd, env, repo
        self.sid = str(uuid.uuid4())

    def hook(self, fixture: str, tool_use_id: str | None = None) -> None:
        text = (FIXTURES / f"{fixture}.json").read_text()
        text = text.replace("<repo>", str(self.repo)).replace(
            "<home>", self.env["HOME"]
        )
        body = json.loads(text)
        body["session_id"] = self.sid
        if tool_use_id is not None:
            body["tool_use_id"] = tool_use_id
        subprocess.run(
            [*self.cmd, "hook"],
            input=json.dumps(body).encode(),
            env=self.env,
            check=True,
            capture_output=True,
            cwd=self.env["HOME"],
        )

    def edit(self) -> None:
        self.hook("pre_tool_use_edit")
        calc = self.repo / "pkg" / "calc.py"
        calc.write_text(calc.read_text().replace("a + b", "b + a"))
        self.hook("post_tool_use_edit")


def commit(s: Session) -> None:
    s.hook("session_start")
    s.hook("user_prompt_submit")
    s.edit()
    s.hook("pre_tool_use_write")
    (s.repo / "pkg" / "extra.py").write_text("X = 1\n")
    s.hook("post_tool_use_write")
    s.hook("pre_tool_use_bash_commit")
    git(s.repo, "add", "-A")
    git(s.repo, "commit", "-qm", "step")
    s.hook("post_tool_use_bash_commit")
    s.hook("stop")


def sed(s: Session) -> None:
    s.hook("session_start")
    s.hook("user_prompt_submit")
    s.hook("pre_tool_use_bash_commit", "toolu_e03b_sed")
    subprocess.run(
        ["sed", "-i", "s/a + b/b + a/", str(s.repo / "pkg" / "calc.py")], check=True
    )
    git(s.repo, "commit", "-qam", "step")
    s.hook("post_tool_use_bash_commit", "toolu_e03b_sed")
    s.hook("stop")


def pull(s: Session) -> None:
    s.hook("session_start")
    s.hook("user_prompt_submit")
    s.edit()
    (s.repo / "owner.txt").write_text("owner\n")
    git(s.repo, "add", "owner.txt")
    git(s.repo, "commit", "-qm", "owner")
    s.hook("stop")


def checkout(s: Session) -> None:
    (s.repo / "pkg" / "two.py").write_text("Y = 2\n")
    git(s.repo, "add", ".")
    git(s.repo, "commit", "-qm", "second")
    s.hook("session_start")
    s.hook("user_prompt_submit")
    git(s.repo, "checkout", "-q", "HEAD~1")
    s.hook("stop")


SCENARIOS = {"commit": commit, "sed": sed, "pull": pull, "checkout": checkout}


Rows = list[tuple[Any, ...]]


@dataclass(frozen=True)
class Obs:
    deltas: Rows
    stop_attributions: Rows
    stop_event: Rows


def observe(home: Path, sid: str) -> Obs:
    db = sqlite3.connect(home / "ledger.db")
    deltas = db.execute(
        "SELECT tool_use_id, file, delta FROM agent_deltas WHERE session_id = ?"
        " ORDER BY id",
        (sid,),
    ).fetchall()
    stop_ts = db.execute(
        "SELECT MAX(ts) FROM attributions WHERE session_id = ? AND trigger = 'Stop'",
        (sid,),
    ).fetchone()[0]
    attributed = db.execute(
        "SELECT file, agent, human, coverage, warnings FROM attributions"
        " WHERE session_id = ? AND trigger = 'Stop' AND ts = ? ORDER BY file",
        (sid, stop_ts),
    ).fetchall()
    stop = db.execute(
        "SELECT outcome, note FROM hook_events WHERE session_id = ? AND event = 'Stop'",
        (sid,),
    ).fetchall()
    db.close()
    return Obs(deltas, attributed, stop)


def run(
    label: str, cmd: list[str], root: Path, src: Path | None = None
) -> dict[str, Obs]:
    """``src`` puts another praxis tree first on the interpreter's path."""
    home = root / f"home-{label}"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PRAXIS_HOME": str(root / f"praxis-{label}"),
        "PATH": os.environ["PATH"],
    }
    if src is not None:
        env["PYTHONPATH"] = str(src)
    out: dict[str, Obs] = {}
    for name, scenario in SCENARIOS.items():
        s = Session(cmd, env, fresh(root, f"{label}-{name}"))
        scenario(s)
        out[name] = observe(Path(env["PRAXIS_HOME"]), s.sid)
    return out


def verdict(after: dict[str, Obs]) -> dict[str, bool]:
    return {name: check(after[name]) for name, check in CHECKS.items()}


def check_commit(c: Obs) -> bool:
    plus = [r for r in c.deltas if r[2] == 1]
    commit_rows = [r for r in c.deltas if r[0] == "toolu_01AvnTJMd17QEZdQQ4S9UpVp"]
    sums = {r[0]: (r[1], r[2], r[3]) for r in c.stop_attributions}
    return (
        not commit_rows
        and len(plus) == len(c.deltas) == 3
        and sums.get("pkg/calc.py") == (2, 0, "full")
        and sums.get("pkg/extra.py") == (1, 0, "full")
    )


def check_sed(d: Obs) -> bool:
    rows = sorted(r[1:] for r in d.deltas if r[0] == "toolu_e03b_sed")
    sums = {r[0]: r[1] for r in d.stop_attributions}
    return rows == [("pkg/calc.py", 1)] * 2 and sums.get("pkg/calc.py") == 2


def check_pull(p: Obs) -> bool:
    marked = [
        r[3] == "partial" and "head_moved_outside_tool" in json.loads(r[4])
        for r in p.stop_attributions
    ]
    sums = {r[0]: r[1] for r in p.stop_attributions}
    return bool(marked) and all(marked) and sums.get("pkg/calc.py") == 2


def check_checkout(k: Obs) -> bool:
    stop = [(o, "head_not_descendant" in (n or "")) for o, n in k.stop_event]
    return stop == [("unobservable", True)]


CHECKS = {
    "commit": check_commit,
    "sed": check_sed,
    "pull": check_pull,
    "checkout": check_checkout,
}


def main(argv: list[str]) -> int:
    before_commit = argv[0] if argv else "e9b5e3a"
    root = Path(tempfile.mkdtemp(prefix="e03b-"))
    try:
        clone = scratch_clone(PRAXIS, root / "scratch-home")
        git(clone, "checkout", "-q", before_commit)
        installed = shutil.which("praxis") or str(PRAXIS / ".venv" / "bin" / "praxis")
        old = [sys.executable, "-c", MAIN]
        before = run("before", old, root, clone / "src")
        after = run("after", [installed], root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    for label, result in (("before (HEAD diffing)", before), ("after", after)):
        print(f"== {label}")
        for name, obs in result.items():
            print(f"  {name}:")
            for key, value in vars(obs).items():
                print(f"    {key}: {value}")
    print("== rule on the before recorder (must fail):", verdict(before))
    checks = verdict(after)
    print("== rule on the installed binary:", checks)
    print("PASS" if all(checks.values()) else "FAIL")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
