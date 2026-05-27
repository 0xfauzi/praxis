"""Generate synthetic chat history for testing.

Creates sessions across multiple days, multiple models, and three
behavioral archetypes (engaged / middling / delegating) so we can
exercise the full pipeline: scoring, trajectory analysis, and the
per-model advisor.

By default writes to a sandbox (`./synthetic_chat_home/`) and prints
the env vars to point the scanners at it. Pass `--in-real-home` to
write to the real `~/.claude/projects` and `~/.codex/sessions` paths
(only if you genuinely want synthetic sessions to appear alongside
your real history — usually you don't).

The sandboxed mode is the safe default. The script never deletes any
existing chat data; it only adds new files with random UUIDs.

Usage:
    python tests/make_synthetic_data.py                  # safe sandbox
    python tests/make_synthetic_data.py --in-real-home   # real ~/.claude/...
"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


PRACTITIONER_PROMPT = (
    "Goal: refactor our auth middleware to use JWT instead of session cookies.\n\n"
    "Constraints:\n"
    "- Keep /login and /logout endpoint signatures unchanged\n"
    "- All current tests must still pass\n"
    "- No breaking changes to the client SDK\n\n"
    "Acceptance: integration tests green, /login returns valid JWT, 401 on expired tokens.\n\n"
    "Here's the current middleware:\n```python\ndef auth_middleware(request):\n"
    "    session_id = request.cookies.get('sid')\n    if not session_id:\n"
    "        return Response(status=401)\n```\n\n"
    "Walk me through the migration plan as JSON: {phases: [{name, changes, risks}]}, "
    "then I'll approve before you touch any code."
)


def make_engaged_session() -> list[tuple[str, str]]:
    return [
        ("user", PRACTITIONER_PROMPT),
        ("assistant", "Here's the migration plan: {phases: [...]}"),
        ("user",
         "Wait — explain why you chose RS256 over HS256. I want to understand "
         "the security trade-off before we lock that in. Also, what does the "
         "refresh token rotation pattern look like and why do we need it?"),
        ("assistant", "RS256 uses asymmetric crypto, HS256 uses shared secret..."),
        ("user",
         "So if I understand: HS256 uses a shared secret, RS256 uses public/"
         "private keys. If we ever need to distribute verification without "
         "trusting other services with signing, RS256 is required. Am I right "
         "that this is the main reason we'd pay the perf cost?"),
        ("assistant", "Exactly right. Should I proceed with phase 1?"),
        ("user",
         "Approved. After each file change, run the test suite and show me "
         "the diff. What's your source for the 15-minute JWT expiry default? "
         "I want to verify before we commit."),
    ]


def make_delegating_session() -> list[tuple[str, str]]:
    return [
        ("user", "write me a python function to parse json"),
        ("assistant", "Here's a function..."),
        ("user", "make it handle errors"),
        ("assistant", "Updated with try/except..."),
        ("user", "now write tests"),
        ("assistant", "Here are the tests..."),
        ("user", "fix this it's broken: TypeError: 'NoneType'"),
        ("assistant", "Try this fix..."),
    ]


def make_middling_session() -> list[tuple[str, str]]:
    return [
        ("user",
         "I'm seeing 'Each child should have a unique key' in my React app.\n"
         "```jsx\nfunction MyList({items}) {\n  return items.map(i => <li>{i}</li>);\n}\n```\n"
         "What's happening?"),
        ("assistant", "React needs a key prop for list reconciliation..."),
        ("user",
         "Got it, that makes sense. So basically React uses keys to figure out "
         "what changed. What happens if I use the array index as the key?"),
        ("assistant", "Index as key works when the list is static..."),
        ("user", "thanks!"),
    ]


def _claude_event(role: str, content: str, ts: datetime, model: str = "claude-opus-4-7") -> dict:
    return {
        "type": role,
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "message": {
            "role": role,
            "content": content,
            "model": model if role == "assistant" else None,
        },
    }


def write_claude_session(
    claude_root: Path,
    turns: list[tuple[str, str]],
    project: str = "myproject",
    when: datetime | None = None,
    model: str = "claude-opus-4-7",
) -> Path:
    project_dir = claude_root / project
    project_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid.uuid4())
    path = project_dir / f"{session_id}.jsonl"
    start = when or (datetime.now(timezone.utc) - timedelta(hours=2))
    with path.open("w", encoding="utf-8") as f:
        for i, (role, content) in enumerate(turns):
            f.write(json.dumps(_claude_event(role, content, start + timedelta(minutes=i * 2), model=model)) + "\n")
    return path


def write_codex_session(
    codex_root: Path,
    turns: list[tuple[str, str]],
    when: datetime | None = None,
    model: str = "gpt-5",
) -> Path:
    when = when or datetime.now(timezone.utc)
    day_dir = codex_root / f"{when.year:04d}" / f"{when.month:02d}" / f"{when.day:02d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    sid = uuid.uuid4().hex[:12]
    path = day_dir / f"rollout-{when.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "session_meta",
            "timestamp": when.isoformat().replace("+00:00", "Z"),
            "payload": {"model": model, "cwd": "/Users/test/repo"},
        }) + "\n")
        for i, (role, content) in enumerate(turns):
            f.write(json.dumps({
                "type": "message",
                "timestamp": (when + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
                "payload": {"role": role, "content": content},
            }) + "\n")
    return path


def generate_corpus(claude_root: Path, codex_root: Path) -> tuple[int, int, int]:
    """Write a 14-day learning trajectory across multiple models. Returns (total, claude, codex)."""
    now = datetime.now(timezone.utc)
    factories = {
        "engaged": make_engaged_session,
        "delegating": make_delegating_session,
        "middling": make_middling_session,
    }

    # Trajectory: starts heavy delegation, ends engaged. Mix of models so the
    # advisor has multiple buckets to read.
    plan: list[tuple] = []
    for days_ago in range(13, -1, -1):
        when = now - timedelta(days=days_ago)
        if days_ago >= 9:
            plan.append(("codex", "delegating", when, "gpt-5"))
            if days_ago % 2 == 0:
                plan.append(("claude", "delegating", when + timedelta(hours=3),
                             "claude-haiku-4-5", "-Users-test-quick"))
        elif days_ago >= 5:
            plan.append(("claude", "middling", when, "claude-sonnet-4-6", "-Users-test-app"))
            plan.append(("codex", "middling", when + timedelta(hours=4), "gpt-5"))
        else:
            plan.append(("claude", "engaged", when, "claude-opus-4-7", "-Users-test-auth"))
            if days_ago < 3:
                plan.append(("claude", "engaged", when + timedelta(hours=5),
                             "claude-sonnet-4-6", "-Users-test-auth"))

    claude_count = 0
    codex_count = 0
    for item in plan:
        provider, kind, when = item[0], item[1], item[2]
        turns = factories[kind]()
        if provider == "claude":
            model = item[3]
            project = item[4]
            write_claude_session(claude_root, turns, project=project, when=when, model=model)
            claude_count += 1
        else:
            model = item[3]
            write_codex_session(codex_root, turns, when=when, model=model)
            codex_count += 1
    return claude_count + codex_count, claude_count, codex_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--in-real-home",
        action="store_true",
        help="Write to real ~/.claude/projects and ~/.codex/sessions (DANGEROUS — only if you want synthetic sessions mixed with real history).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent.parent / "synthetic_chat_home",
        help="Output directory for sandboxed synthetic data (default: ./synthetic_chat_home/).",
    )
    args = parser.parse_args()
    out_dir: Path = args.out if isinstance(args.out, Path) else Path(args.out)

    scorecard_home: Path | None = None
    if args.in_real_home:
        claude_root = Path.home() / ".claude" / "projects"
        codex_root = Path.home() / ".codex" / "sessions"
        print("WARNING: writing synthetic sessions into your real chat dirs.")
        print(f"  Claude: {claude_root}")
        print(f"  Codex:  {codex_root}")
    else:
        sandbox = out_dir.resolve()
        sandbox.mkdir(parents=True, exist_ok=True)
        claude_root = sandbox / "claude" / "projects"
        codex_root = sandbox / "codex" / "sessions"
        scorecard_home = sandbox / "scorecard_home"
        scorecard_home.mkdir(parents=True, exist_ok=True)
        print(f"Sandbox: {sandbox}")

    total, claude_count, codex_count = generate_corpus(claude_root, codex_root)
    print(f"Generated {total} synthetic sessions across 14 days "
          f"({claude_count} Claude, {codex_count} Codex)")

    if scorecard_home is not None:
        print()
        print("To scan this sandbox, point the CLI at it via env vars:")
        print()
        print(f"  export PRAXIS_CLAUDE_ROOT={claude_root}")
        print(f"  export PRAXIS_CODEX_HOME={codex_root.parent}")
        print(f"  export PRAXIS_HOME={scorecard_home}")
        print(f"  praxis scan")
        print()
        print("Or one-liner:")
        print()
        print(f"  PRAXIS_CLAUDE_ROOT={claude_root} \\")
        print(f"  PRAXIS_CODEX_HOME={codex_root.parent} \\")
        print(f"  PRAXIS_HOME={scorecard_home} \\")
        print(f"  praxis scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
