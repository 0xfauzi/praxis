"""CLI entry point.

Commands:
  week             Render this week's digest (default verb in v0.2).
  scan             Run a scan + score + consolidate cycle. The 'main' verb.
  report           Open or print the latest HTML report.
  status           Show what's been scored, when, and where.
  rubric           Print the scoring rubric and weights.
  follow-up        Print the most recent weekly commitment and its outcome.
  install-daemon   Print platform-specific scheduling instructions.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path

from praxis import __version__
from praxis.config import ensure_config_file
from praxis.orchestrator import InvalidWeekError, run, run_weekly
from praxis.reports.html_report import render as render_html
from praxis.reports.terminal import render as render_terminal
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore, resolve_home


def _weekly_html_path(week_iso: str) -> Path:
    """Per-ISO-week HTML path (spec section 13.1).

    The macOS launchd job writes one HTML file per week here so the
    user can re-open past weeks; ``~/.praxis/latest.html`` is a separate
    symlink target managed by the scheduled run (out of scope here).
    """
    weeks_dir = resolve_home() / "weeks"
    weeks_dir.mkdir(parents=True, exist_ok=True)
    return weeks_dir / f"{week_iso}.html"


def _post_notify(week_iso: str, trajectory_label: str | None) -> None:
    """Best-effort macOS notification (spec section 13.2).

    Silent no-op on non-macOS so the same flag is portable. ``osascript``
    failures (notifications disabled, sandboxed env) are logged to stderr
    and do not fail the run -- the digest is still rendered.
    """
    if sys.platform != "darwin":
        return
    title = "Praxis weekly read is ready"
    body = f"Open ~/.praxis/weeks/{week_iso}.html to read."
    if trajectory_label:
        title = f"Praxis: {trajectory_label} this week"
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{body}" with title "{title}" sound name "default"',
            ],
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[cli] osascript notification failed: {exc!r}", file=sys.stderr)


def cmd_week(args: argparse.Namespace) -> int:
    """Render this week's digest (or a past week with --week <iso>).

    Flag behavior (spec sections 12.1, 13.1-13.3):
      --week <iso>      Render the persisted data for that past ISO week
                        (e.g. ``2026-W21``). Scanning and scoring are
                        skipped; the snapshot is rebuilt from the DB so
                        the digest reflects what was judged at the time.
      --dry-run         Compute the digest from existing data only; do
                        not invoke the scan+score+persist path and do
                        not write any files. Useful for previewing the
                        digest layout against current data.
      --frontier-only   Force every session through the frontier judge
                        (spec 9.6). The flag is wired to the orchestrator
                        today so the seam stays stable; the two-pass
                        judge that honors it lands in a separate story.
      --explain-judging Print the pass-1 confidence distribution at the
                        end of the run (spec 9.6). With the two-pass
                        judge not yet wired, the explainer says so
                        explicitly rather than fabricating numbers.
      --notify          Post a macOS notification when the digest is
                        ready. Silent no-op on non-Darwin platforms.
      --write-html      Write the HTML digest to ``~/.praxis/weeks/
                        <iso>.html``. The terminal render always happens;
                        the HTML file is written only when this flag (or
                        the scheduled --notify run) requests it.
    """
    try:
        summary = run_weekly(
            week_iso=args.week,
            dry_run=args.dry_run,
            frontier_only=args.frontier_only,
            explain_judging=args.explain_judging,
        )
    except InvalidWeekError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(render_terminal(summary))

    target_week = summary.week_iso or "current"
    if args.write_html or args.notify:
        html_path = _weekly_html_path(target_week)
        html_path.write_text(render_html(summary), encoding="utf-8")
        print(f"  HTML saved: {html_path}")

    if args.explain_judging:
        print()
        print("  --explain-judging:")
        if summary.forced_frontier:
            print("    pass-1 skipped (--frontier-only forced frontier judge).")
        confidence = summary.judging_confidence or {}
        if confidence:
            for label in ("high", "medium", "low"):
                if label in confidence:
                    print(f"    {label}: {confidence[label]}")
        else:
            print(
                "    no pass-1 confidence recorded for this run "
                "(two-pass judge wiring lands in a later story)."
            )

    if args.notify:
        traj_label = (
            summary.trajectory.label.value.title()
            if summary.trajectory is not None
            else None
        )
        _post_notify(target_week, traj_label)

    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    summary = run(
        since_days=args.since_days,
        max_new_scored=args.max_new,
        force_consolidate=args.force_consolidate,
    )

    # Terminal output always.
    print(render_terminal(summary))

    # HTML report.
    html_path = resolve_home() / "report.html"
    html_path.write_text(render_html(summary), encoding="utf-8")
    print(f"  Report saved: {html_path}")

    if args.open:
        webbrowser.open(html_path.as_uri())

    return 0


def cmd_report(args: argparse.Namespace) -> int:
    html_path = resolve_home() / "report.html"
    if not html_path.exists():
        print("No report yet. Run: praxis scan")
        return 1
    if args.print:
        print(html_path.read_text(encoding="utf-8"))
    else:
        webbrowser.open(html_path.as_uri())
        print(f"Opened: {html_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    store = ProfileStore()
    rows = store.load_session_scores()
    history = store.consolidation_history(days=30)
    print(f"Scorecard home: {resolve_home()}")
    print(f"Sessions scored: {len(rows)}")
    if rows:
        print(f"  First: {rows[0]['started_at']}")
        print(f"  Most recent: {rows[-1]['started_at']}")
        providers: dict[str, int] = {}
        for r in rows:
            providers[r["provider"]] = providers.get(r["provider"], 0) + 1
        for prov, count in sorted(providers.items(), key=lambda kv: -kv[1]):
            print(f"  {prov}: {count}")
    print(f"Daily consolidations in last 30 days: {len(history)}")
    if history:
        print(f"  Latest: {history[-1]['consolidation_date']} "
              f"({history[-1]['snapshot']['overall']:.1f}/10)")
    return 0


def cmd_follow_up(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Print the most recent weekly commitment status.

    Exit codes:
      0 -- a follow-up row exists and was printed.
      3 -- no follow-up has been recorded yet (no weekly digest has run).
    """
    store = ProfileStore()
    fu = store.latest_follow_up()
    if fu is None:
        print("No follow-up yet. Run a weekly digest first to record a commitment.")
        return 3

    measured = f"{fu.measured_value:.2f}" if fu.measured_value is not None else "--"
    print(f"Week:       {fu.week_iso}")
    print(f"Dimension:  {fu.dim_key}")
    print(f"Metric:     {fu.target_metric}")
    print("Commitment:")
    print(f"  {fu.commitment_text}")
    print()
    print(f"Baseline:   {fu.baseline_value:.2f}")
    print(f"Measured:   {measured}")
    print(f"Outcome:    {fu.outcome}")
    return 0


def cmd_rubric(args: argparse.Namespace) -> int:  # noqa: ARG001
    print("\nPRAXIS — SCORING RUBRIC\n")
    for d in RUBRIC:
        print(f"  {d.title}  ({int(d.weight*100)}%)")
        print(f"    {d.description}")
        print(f"    Evidence: {d.evidence}")
        print()
    return 0


def cmd_install_daemon(args: argparse.Namespace) -> int:  # noqa: ARG001
    import platform

    system = platform.system()
    home = Path.home()
    cmd = "praxis scan"

    if system == "Darwin":
        plist_path = home / "Library" / "LaunchAgents" / "co.praxis.plist"
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>co.praxis</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-lc</string><string>{cmd}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>18</integer>
    <key>Minute</key><integer>30</integer>
  </dict>
  <key>StandardOutPath</key><string>{home}/.praxis/daemon.log</string>
  <key>StandardErrorPath</key><string>{home}/.praxis/daemon.err.log</string>
</dict>
</plist>"""
        print("macOS — install LaunchAgent (runs daily at 18:30):")
        print(f"\n  Save the following to: {plist_path}")
        print("  Then: launchctl load -w " + str(plist_path))
        print("\n--- plist contents ---")
        print(plist)
    elif system == "Linux":
        print("Linux — install systemd user timer (runs daily at 18:30):")
        unit_dir = home / ".config" / "systemd" / "user"
        print(f"\n  mkdir -p {unit_dir}")
        print(f"  # save the following two files in {unit_dir}/")
        print("\n--- praxis.service ---")
        print(f"""[Unit]
Description=Praxis
[Service]
Type=oneshot
ExecStart=/bin/sh -lc '{cmd}'
""")
        print("--- praxis.timer ---")
        print("""[Unit]
Description=Daily Praxis run
[Timer]
OnCalendar=*-*-* 18:30:00
Persistent=true
[Install]
WantedBy=timers.target
""")
        print("Then:")
        print("  systemctl --user daemon-reload")
        print("  systemctl --user enable --now praxis.timer")
    elif system == "Windows":
        print("Windows — Task Scheduler (daily at 18:30):")
        print(f"""
  schtasks /Create /SC DAILY /TN "Praxis" /TR "{cmd}" /ST 18:30
""")
    else:
        print(f"Unknown system {system}. Schedule `{cmd}` daily with your OS tools.")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    from praxis.config_cli import (
        ConfigCLIError,
        get_value,
        open_editor,
        set_value,
    )

    try:
        if args.get is not None:
            print(get_value(args.get))
            return 0
        if args.set is not None:
            if "=" not in args.set:
                print(
                    f"Invalid --set argument {args.set!r}: "
                    f"expected 'key=value' (e.g., schedule.day=monday).",
                    file=sys.stderr,
                )
                return 1
            key, raw = args.set.split("=", 1)
            set_value(key, raw)
            return 0
        return open_editor()
    except ConfigCLIError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_models(args: argparse.Namespace) -> int:
    from praxis.models_advisor import load_all_cards
    from praxis.models_advisor.cards import find_card_for_model_hint

    cards = load_all_cards()
    if args.show:
        # Use the same resolver scanners use, so "Claude Opus 4.7" / dotted
        # variants / aliases all work consistently.
        card = find_card_for_model_hint(args.show)
        if card is None:
            print(f"No card found for '{args.show}'.")
            return 1
        print(f"\n{card.display_name}  ({card.vendor} · {card.tier})")
        print(f"  ID: {card.id}")
        print(f"  Context window: {card.context_window_tokens:,} tokens")
        if card.input_per_million_usd is not None:
            print(f"\n  Pricing (verified {card.pricing_last_verified or 'unknown'}):")
            print(f"    · Input:  ${card.input_per_million_usd:g} / million tokens")
            print(f"    · Output: ${card.output_per_million_usd:g} / million tokens")
            if card.pricing_notes:
                print(f"    · Notes:  {card.pricing_notes}")
            if card.pricing_source:
                print(f"    · Verify: {card.pricing_source}")
        else:
            print("\n  Pricing: subscription-based (no per-token rate)")
            if card.pricing_source:
                print(f"    · See: {card.pricing_source}")
        print("\n  Strengths:")
        for s in card.strengths:
            print(f"    · {s}")
        print("\n  Weaknesses:")
        for s in card.weaknesses:
            print(f"    · {s}")
        print("\n  Best for:")
        for s in card.best_for:
            print(f"    · {s}")
        print("\n  Avoid for:")
        for s in card.avoid_for:
            print(f"    · {s}")
        print("\n  Prompting quirks:")
        for s in card.prompting_quirks:
            print(f"    · {s}")
        if card.notes:
            print(f"\n  Notes: {card.notes}")
        if card.sources:
            print("\n  Sources (verifiable docs):")
            for src in card.sources:
                print(f"    · {src}")
        return 0

    # Default: list all cards
    print(f"\n{len(cards)} model cards loaded\n")
    by_family: dict[str, list] = {}
    for card in cards.values():
        by_family.setdefault(card.family, []).append(card)
    for family in sorted(by_family.keys()):
        print(f"  {family.upper()}")
        for card in sorted(by_family[family], key=lambda c: c.tier):
            print(f"    {card.id.ljust(28)} {card.tier.ljust(12)} {card.display_name}")
        print()
    print(f"Custom cards: drop JSON files in {resolve_home() / 'model_cards'}")
    print(f"Inspect one card: praxis models --show <id>")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="praxis",
        description="Scan your Claude / Codex / Copilot chat history "
                    "and score your AI usage against research-backed criteria.",
    )
    p.add_argument("--version", action="version", version=f"praxis {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    week = sub.add_parser(
        "week",
        help="Render this week's digest (the v0.2 primary verb).",
        description=(
            "Render the weekly digest from the current data, or render a "
            "past week with --week <iso>. The terminal output always "
            "renders; HTML and notifications are opt-in via flags."
        ),
    )
    week.add_argument(
        "--week",
        type=str,
        default=None,
        metavar="ISO",
        help=(
            "Render a past ISO week (e.g. 2026-W21). Scanning and scoring "
            "are skipped; the snapshot is rebuilt from the persisted DB rows."
        ),
    )
    week.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compute everything but do not write to the DB or any files. "
            "Useful for previewing the digest against current data."
        ),
    )
    week.add_argument(
        "--frontier-only",
        action="store_true",
        help=(
            "Force every session through the frontier judge (spec 9.6). "
            "The two-pass judge wiring lands in a separate story; the "
            "flag is wired here so the CLI seam stays stable."
        ),
    )
    week.add_argument(
        "--explain-judging",
        action="store_true",
        help=(
            "Print the pass-1 confidence distribution after the digest "
            "(spec 9.6). Will note when no distribution was recorded."
        ),
    )
    week.add_argument(
        "--notify",
        action="store_true",
        help=(
            "Post a macOS notification when the digest is ready. Silent "
            "no-op on non-Darwin platforms (spec 13.2). Implies "
            "--write-html."
        ),
    )
    week.add_argument(
        "--write-html",
        action="store_true",
        help=(
            "Write the HTML digest to ~/.praxis/weeks/<iso>.html "
            "(spec 13.1). The terminal render is always printed."
        ),
    )
    week.set_defaults(func=cmd_week)

    scan = sub.add_parser("scan", help="Scan, score, and consolidate.")
    scan.add_argument("--since-days", type=int, default=30,
                      help="Only consider session files modified in the last N days.")
    scan.add_argument("--max-new", type=int, default=50,
                      help="Cap on newly-discovered sessions to deep-score per run.")
    scan.add_argument("--force-consolidate", action="store_true",
                      help="Re-run daily consolidation even if already done today.")
    scan.add_argument("--open", action="store_true",
                      help="Open the HTML report after scanning.")
    scan.set_defaults(func=cmd_scan)

    rep = sub.add_parser("report", help="Open or print the latest HTML report.")
    rep.add_argument("--print", action="store_true",
                     help="Print HTML to stdout instead of opening browser.")
    rep.set_defaults(func=cmd_report)

    sts = sub.add_parser("status", help="Show current scorecard status.")
    sts.set_defaults(func=cmd_status)

    rub = sub.add_parser("rubric", help="Print the scoring rubric.")
    rub.set_defaults(func=cmd_rubric)

    fup = sub.add_parser(
        "follow-up",
        help="Show the most recent weekly commitment and its outcome.",
    )
    fup.set_defaults(func=cmd_follow_up)

    daem = sub.add_parser("install-daemon",
                          help="Print scheduler config for running daily.")
    daem.set_defaults(func=cmd_install_daemon)

    mod = sub.add_parser("models",
                         help="List model cards or show one in detail.")
    mod.add_argument("--show", type=str, default=None,
                     help="Show full details for one model card (by id or alias).")
    mod.set_defaults(func=cmd_models)

    cfg = sub.add_parser("config",
                         help="View, --get, or --set ~/.praxis/config.toml.")
    cfg_action = cfg.add_mutually_exclusive_group()
    cfg_action.add_argument("--get", type=str, default=None, metavar="KEY",
                            help="Print the value of a dotted key "
                                 "(e.g., schedule.day).")
    cfg_action.add_argument("--set", type=str, default=None, metavar="KEY=VALUE",
                            help="Set a dotted key, preserving the rest of "
                                 "the file (e.g., schedule.day=monday).")
    cfg.set_defaults(func=cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ensure_config_file()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
