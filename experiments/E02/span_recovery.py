"""Can agent-authored line spans be recovered from the ledger's hashes alone?

Runs the recorder through the recorded Edit against a real repo, then hashes every line of
the CURRENT file as a '+' line and looks each up in agent_deltas. Expected: exactly the
agent's three new lines (12-14 after the edit) are flagged, nothing else, and the span
maps to one function (divide). Then the owner appends a function and the agent lines are
still found at the same place (line numbers, not hashes, are what moved)."""
import json, os, shutil, sqlite3, subprocess, sys, tempfile, uuid
from pathlib import Path
sys.path.insert(0, "/home/user/praxis/src")
from praxis.gitdiff import line_hash
FIX = Path("/home/user/praxis/fixtures/claude/2.1.257")
body = json.loads((FIX / "post_tool_use_edit.json").read_text())
ORIG = body["tool_response"]["originalFile"]; AFTER = ORIG.replace(body["tool_response"]["oldString"], body["tool_response"]["newString"], 1)
root = Path(tempfile.mkdtemp()); home = root/"home"; ph = root/"ph"; repo = root/"repo"; home.mkdir(); (repo/"pkg").mkdir(parents=True)
(repo/"pkg"/"calc.py").write_text(ORIG)
for a in (("init","-q"),("config","user.email","t@x.invalid"),("config","user.name","t"),("add","."),("commit","-q","-m","b")):
    subprocess.run(["git","-C",str(repo),*a],check=True,capture_output=True)
env = {"HOME": str(home), "PRAXIS_HOME": str(ph), "PATH": os.environ["PATH"]}; sid = str(uuid.uuid4())
def hook(name, **o):
    t = (FIX/f"{name}.json").read_text().replace("<repo>", str(repo)).replace("<home>", str(home)); b = json.loads(t); b["session_id"]=sid; b["cwd"]=str(repo); b.update(o)
    subprocess.run([shutil.which("praxis"),"hook"], input=json.dumps(b).encode(), env=env, check=True, capture_output=True)
hook("pre_tool_use_edit"); (repo/"pkg"/"calc.py").write_text(AFTER); hook("post_tool_use_edit")
db = sqlite3.connect(ph/"ledger.db")
agent = {h for (h,) in db.execute("SELECT line_hash FROM agent_deltas WHERE session_id=? AND file='pkg/calc.py' AND delta>0", (sid,))}
def flagged(text):
    return [i+1 for i, line in enumerate(text.encode().split(b"\n")[:-1]) if line_hash(b"+", line) in agent]
f1 = flagged(AFTER); print("agent-authored lines after the edit:", f1)
after2 = AFTER + "\n\ndef owner_added() -> int:\n    return 42\n"
f2 = flagged(after2); print("after the owner appends a function:", f2)
# which function holds the span? a tiny def-scan is enough for the proof
import ast
tree = ast.parse(AFTER); owners = {n.name: (n.lineno, n.end_lineno) for n in tree.body if isinstance(n, ast.FunctionDef)}
print("functions:", owners); print("span owner:", [name for name,(a,b) in owners.items() if all(a <= ln <= b for ln in f1)])
assert f1 == [12,13,14] and f2 == [12,13,14], (f1, f2); print("OK")
