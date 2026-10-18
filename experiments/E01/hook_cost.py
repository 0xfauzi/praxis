"""Live cost of `praxis hook`: 20 sessions x 5 events against a real repo, wall-clock
around the whole process (what the agent waits for) and the in-process duration_ms the
hook recorded about itself."""
import json, os, shutil, sqlite3, statistics, subprocess, tempfile, time, uuid
from pathlib import Path
FIX = Path("/home/user/praxis/fixtures/claude/2.1.257")
body = json.loads((FIX / "post_tool_use_edit.json").read_text())
ORIG = body["tool_response"]["originalFile"]; AFTER = ORIG.replace(body["tool_response"]["oldString"], body["tool_response"]["newString"], 1)
praxis = shutil.which("praxis")
root = Path(tempfile.mkdtemp()); home = root / "home"; ph = root / "praxis-home"; home.mkdir()
env = {"HOME": str(home), "PRAXIS_HOME": str(ph), "PATH": os.environ["PATH"]}
def payload(name, repo, sid):
    t = (FIX / f"{name}.json").read_text().replace("<repo>", str(repo)).replace("<home>", str(home))
    b = json.loads(t); b["session_id"] = sid; b["cwd"] = str(repo); return b
wall = {}
for i in range(20):
    repo = root / f"repo{i}"; (repo / "pkg").mkdir(parents=True); (repo / "pkg" / "calc.py").write_text(ORIG)
    for a in (("init","-q"),("config","user.email","t@x.invalid"),("config","user.name","t"),("add","."),("commit","-q","-m","b")):
        subprocess.run(["git","-C",str(repo),*a],check=True,capture_output=True)
    sid = str(uuid.uuid4())
    seq = [("SessionStart", {"session_id": sid, "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"}),
           ("UserPromptSubmit", {"session_id": sid, "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "x"}),
           ("PreToolUse", payload("pre_tool_use_edit", repo, sid)), ("PostToolUse", payload("post_tool_use_edit", repo, sid)),
           ("Stop", payload("stop", repo, sid))]
    for ev, p in seq:
        if ev == "PostToolUse": (repo / "pkg" / "calc.py").write_text(AFTER)
        t = time.perf_counter(); subprocess.run([praxis, "hook"], input=json.dumps(p).encode(), env=env, check=True, capture_output=True)
        wall.setdefault(ev, []).append((time.perf_counter() - t) * 1000)
db = sqlite3.connect(ph / "ledger.db")
inner = {}
for ev, d in db.execute("SELECT event, duration_ms FROM hook_events"): inner.setdefault(ev, []).append(d)
def pct(xs, p): xs = sorted(xs); return xs[min(len(xs)-1, round(p/100*(len(xs)-1)))]
print(f"{'event':18} {'n':>3} {'wall p50':>9} {'wall p95':>9} {'wall max':>9} | {'inner p50':>9} {'inner p95':>9}")
for ev in ("SessionStart","UserPromptSubmit","PreToolUse","PostToolUse","Stop"):
    w, n = wall[ev], inner[ev]
    print(f"{ev:18} {len(w):3d} {pct(w,50):9.1f} {pct(w,95):9.1f} {max(w):9.1f} | {pct(n,50):9.1f} {pct(n,95):9.1f}")
allw = [x for v in wall.values() for x in v]
print(f"all events: n={len(allw)} wall p50 {pct(allw,50):.1f} ms, p95 {pct(allw,95):.1f} ms, max {max(allw):.1f} ms")
print("outcomes:", dict(db.execute("SELECT outcome, COUNT(*) FROM hook_events GROUP BY outcome").fetchall()))
