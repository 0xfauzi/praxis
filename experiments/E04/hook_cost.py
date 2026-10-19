"""E04: what the design 03 recorder duties add to `praxis hook`. Extends E01/hook_cost.py.

Acceptance rule, written before the run (2026-10-19): on a scratch clone of praxis's own
tree, with a frozen mechanical map and a registered task, the Stop that scans X1 edges
and closes the segment must add at most 100 ms to the wall-clock p95 of the same Stop
without a frozen map, and every such Stop must end `ok` with `task closed` in its note.
100 ms is the tolerance chosen beforehand: it doubles neither E01's 92.3 ms p95 nor the
10 s hook timeout's margin. A second check, reported but not part of the rule: the same
Stop with 200,000 extra hook_events rows in the ledger (hook_events has no session
index; the base and HEAD-move reads scan it).

Conditions, 20 sessions each, the installed binary, one process per event:
  E01   the E01 sequence on the small calc repository (this machine, today)
  plain praxis clone, no frozen map: SessionStart, UserPromptSubmit, Pre/Post Edit
        appending one line to src/praxis/cli.py, Stop
  duty  the same with a frozen map and a task registered before the prompt
  big   duty, after 200,000 synthetic hook_events rows from other sessions
"""

import json, os, shutil, sqlite3, subprocess, tempfile, time, uuid
from pathlib import Path

from praxis import components, gitdiff, store

PRAXIS = Path("/home/user/praxis")
FIX = PRAXIS / "fixtures" / "claude" / "2.1.257"
body = json.loads((FIX / "post_tool_use_edit.json").read_text())
ORIG = body["tool_response"]["originalFile"]
AFTER = ORIG.replace(body["tool_response"]["oldString"], body["tool_response"]["newString"], 1)
PAST = "2000-01-01T00:00:00.000Z"
praxis = shutil.which("praxis")
root = Path(tempfile.mkdtemp(prefix="e04-"))
home = root / "home"
home.mkdir()


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout


def payload(name, repo, sid, **over):
    t = (FIX / f"{name}.json").read_text().replace("<repo>", str(repo)).replace("<home>", str(home))
    b = json.loads(t)
    b.update(session_id=sid, cwd=str(repo), **over)
    return b


def run_session(repo, ph, target, after_text, wall):
    env = {"HOME": str(home), "PRAXIS_HOME": str(ph), "PATH": os.environ["PATH"]}
    sid = str(uuid.uuid4())
    seq = [
        ("SessionStart", {"session_id": sid, "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"}),
        ("UserPromptSubmit", {"session_id": sid, "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "x", "prompt_id": sid}),
        ("PreToolUse", payload("pre_tool_use_edit", repo, sid, tool_use_id=f"t-{sid}")),
        ("PostToolUse", payload("post_tool_use_edit", repo, sid, tool_use_id=f"t-{sid}")),
        ("Stop", payload("stop", repo, sid)),
    ]
    for ev, p in seq:
        if ev == "PostToolUse":
            target.write_text(after_text(target.read_text()))
        t = time.perf_counter()
        done = subprocess.run([praxis, "hook"], input=json.dumps(p).encode(), env=env, check=True, capture_output=True)
        wall.setdefault(ev, []).append((time.perf_counter() - t) * 1000)
    return sid, done.stdout


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def condition(name, repo, ph, target, after_text, before_each=None, n=20):
    wall, sids = {}, []
    for i in range(n):
        if before_each:
            before_each(i)
        sid, out = run_session(repo, ph, target, after_text, wall)
        sids.append(sid)
        git(repo, "checkout", "-q", "--", ".")
    db = sqlite3.connect(ph / "ledger.db")
    inner, notes = {}, []
    for ev, d, note, outcome in db.execute(
        "SELECT event, duration_ms, note, outcome FROM hook_events WHERE session_id IN"
        " (SELECT value FROM json_each(?))", (json.dumps(sids),)
    ):
        inner.setdefault(ev, []).append(d)
        if ev == "Stop":
            notes.append((outcome, note))
    print(f"\n[{name}]")
    print(f"{'event':18} {'n':>3} {'wall p50':>9} {'wall p95':>9} {'wall max':>9} | {'inner p50':>9} {'inner p95':>9}")
    for ev in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
        w, k = wall[ev], inner[ev]
        print(f"{ev:18} {len(w):3d} {pct(w, 50):9.1f} {pct(w, 95):9.1f} {max(w):9.1f} | {pct(k, 50):9.1f} {pct(k, 95):9.1f}")
    print("Stop notes:", sorted(set(notes))[:4], "count", len(notes))
    return wall, inner, notes


def small_repo():
    repo = root / "calc"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "calc.py").write_text(ORIG)
    for a in (("init", "-q"), ("config", "user.email", "t@x.invalid"), ("config", "user.name", "t"), ("add", "."), ("commit", "-q", "-m", "b")):
        git(repo, *a)
    return repo


def praxis_clone(tag):
    repo = root / f"praxis-{tag}"
    subprocess.run(["git", "clone", "-q", "--local", str(PRAXIS), str(repo)], check=True)
    return repo


def append_line(text):
    return text + "\n_E04 = 1\n"


# E01, today
calc = small_repo()
condition("E01", calc, root / "ph-e01", calc / "pkg" / "calc.py", lambda _t: AFTER)

# plain: praxis tree, no frozen map
plain = praxis_clone("plain")
w_plain, i_plain, _ = condition("plain", plain, root / "ph-plain", plain / "src/praxis/cli.py", append_line)

# duty: frozen map and a registered task per session
duty = praxis_clone("duty")
ph = root / "ph-duty"
top = gitdiff.toplevel(duty)
cmap = components.derive(duty, gitdiff.repo_id(top))
led = store.Store(ph / "ledger.db").open()
led.component_map_freeze(cmap.repo_id, led.component_map_put(components.to_json(cmap), PAST), PAST)
led.close()
print("map:", cmap.version, len(components.product_components(cmap)), "product components")


def register(i, ph=ph, base=0):
    db = store.Store(ph / "ledger.db").open()
    db.task_register(region=[], bound=[], related=[], drawn=[], task_id=f"task-{base + i}", repo_id=cmap.repo_id,
                     map_version=cmap.version, ordinal=base + i + 1, ts=PAST, arm="control", seed_hash="ab" * 32, stratum="empty")
    db.close()


w_duty, i_duty, notes = condition("duty", duty, ph, duty / "src/praxis/cli.py", append_line, register)

# big: the same ledger after 200,000 filler hook_events rows
db = sqlite3.connect(ph / "ledger.db")
db.executemany(
    "INSERT INTO hook_events (session_id, event, ts, duration_ms, outcome, head_sha) VALUES (?, ?, ?, 1.0, 'ok', ?)",
    ((f"filler-{k // 50}", "PreToolUse", PAST, "0" * 40) for k in range(200_000)),
)
db.commit()
db.close()
w_big, i_big, notes_big = condition("big", duty, ph, duty / "src/praxis/cli.py", append_line, lambda i: register(i, base=20))

added_p95 = pct(w_duty["Stop"], 95) - pct(w_plain["Stop"], 95)
closed = sum(1 for o, n in notes if o == "ok" and n and "task closed" in n)
print(f"\nStop wall p95: plain {pct(w_plain['Stop'], 95):.1f} ms, duty {pct(w_duty['Stop'], 95):.1f} ms, added {added_p95:.1f} ms")
print(f"Stop inner p50: plain {pct(i_plain['Stop'], 50):.1f} ms, duty {pct(i_duty['Stop'], 50):.1f} ms, big {pct(i_big['Stop'], 50):.1f} ms")
print(f"duty Stops closed ok: {closed} of {len(notes)}")
print("RULE:", "PASS" if added_p95 <= 100 and closed == len(notes) == 20 else "FAIL")
shutil.rmtree(root)
