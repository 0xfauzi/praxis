"""CLI entry point.

Commands:
  scan             Run a scan + score + consolidate cycle. The 'main' verb.
  report           Open or print the latest HTML report.
  status           Show what's been scored, when, and where.
  rubric           Print the scoring rubric and weights.
  install-daemon   Print platform-specific scheduling instructions.
"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from praxis import __version__
from praxis.orchestrator import run
from praxis.reports.html_report import render as render_html
from praxis.reports.terminal import render as render_terminal
from praxis.scoring.rubric import RUBRIC
from praxis.storage.profile_store import ProfileStore, resolve_home


def cmd_scan(args: argparse.Namespace) -> int:
    summary = run(
        use_judge=not args.no_judge,
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

    scan = sub.add_parser("scan", help="Scan, score, and consolidate.")
    scan.add_argument("--no-judge", action="store_true",
                      help="Skip LLM judge calls; use heuristics only.")
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

    daem = sub.add_parser("install-daemon",
                          help="Print scheduler config for running daily.")
    daem.set_defaults(func=cmd_install_daemon)

    mod = sub.add_parser("models",
                         help="List model cards or show one in detail.")
    mod.add_argument("--show", type=str, default=None,
                     help="Show full details for one model card (by id or alias).")
    mod.set_defaults(func=cmd_models)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
