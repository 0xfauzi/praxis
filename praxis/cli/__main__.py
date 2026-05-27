"""CLI entry point.

Commands (v0.2 surface):
  week             Render this week's digest (the primary verb in v0.2).
  scan             Scan + score new sessions; no digest rendered (spec 12.1).
  re-score         Re-run the frontier judge for one session and update its row.
  baseline         Print the current 90-day baseline (read-only).
  follow-up        Print the most recent weekly commitment and its outcome.
  history          List past weekly digests by ISO week (read-only).
  show             Render a past week's digest from persisted data (read-only).
  report           Open or print the latest HTML report.
  status           Show what's been scored, when, and where.
  rubric           Print the scoring rubric and weights.
  models           List or describe the built-in model cards.
  config           View / --get / --set ~/.praxis/config.toml.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from praxis import __version__
from praxis.config import ensure_config_file
from praxis.orchestrator import (
    NO_API_KEY_MESSAGE,
    InvalidWeekError,
    ReScoreError,
    has_api_key_configured,
    list_persisted_weeks,
    no_sessions_message,
    re_score_session,
    run,
    run_weekly,
)
from praxis.reports.html_report import render as render_html
from praxis.reports.terminal import render as render_terminal
from praxis.scoring.baseline import (
    BaselineInputSession,
    compute_baseline,
    is_baseline_forming,
)
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

    Exit codes (spec 12.3):
      0  digest rendered.
      1  malformed --week ISO string.
      2  current-week run requested but no API key configured
         (read-only ``--week`` and ``--dry-run`` paths skip this check
         since they never invoke the judge).
      3  zero sessions in the targeted window. Short-circuits before
         rendering / writing HTML / posting a notification so an empty
         digest is never produced as a side effect.
    """
    needs_judge = args.week is None and not args.dry_run
    if needs_judge and not has_api_key_configured():
        print(NO_API_KEY_MESSAGE, file=sys.stderr)
        return 2

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

    if summary.snapshot.session_count == 0:
        print(no_sessions_message(summary.week_iso), file=sys.stderr)
        return 3

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
    """Scan source files and score newly-discovered sessions.

    Per spec 12.1 the v0.2 ``scan`` verb is intentionally NOT a digest
    renderer: it does the work of discovering new sessions and persisting
    judge results, and prints a one-line summary of what changed. The
    digest (terminal masthead, dimensions, coaching, trajectory) is the
    job of ``praxis week`` and ``praxis show <week_iso>``.

    The output is a compact progress report so the user can confirm the
    scan made progress and, if invoked from a cron job, the log lines
    are still grep-able.

    Exit codes (spec 12.3):
      0  scan completed.
      2  no API key configured (scan always calls the judge).
    """
    if not has_api_key_configured():
        print(NO_API_KEY_MESSAGE, file=sys.stderr)
        return 2

    summary = run(
        since_days=args.since_days,
        max_new_scored=args.max_new,
        force_consolidate=args.force_consolidate,
    )

    print(
        f"Scanned {summary.sessions_seen} session(s); "
        f"{summary.sessions_new} new; "
        f"scored {summary.sessions_scored} via judge "
        f"({summary.elapsed_seconds}s)."
    )
    print(f"Render the digest with: praxis week")
    return 0


def cmd_re_score(args: argparse.Namespace) -> int:
    """Re-run the frontier judge against one persisted session.

    Looks up the row in ``session_scores`` by stable_id, re-parses the
    source file via the matching scanner, runs the judge, and overwrites
    the persisted row + moments. Useful when judge prompts or model
    versions change and the user wants to refresh a specific session
    without re-scanning the whole window.

    Exit codes:
      0 - row re-scored and saved
      1 - session_stable_id not found, or the source file could not be parsed
      2 - no judge available (no API keys configured, or all judges errored)
    """
    try:
        score = re_score_session(args.session_stable_id)
    except ReScoreError as exc:
        print(str(exc), file=sys.stderr)
        return 2 if exc.code == "no_judge" else 1
    print(
        f"Re-scored {score.session_stable_id} "
        f"({score.provider}): overall {score.overall:.2f}/10."
    )
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Print the current 90-day baseline of session scores.

    Read-only: never scans, never scores, never writes. Reads the
    persisted ``session_scores`` rows and computes the same Baseline
    the weekly digest would render in its baseline panel. When the
    user has less than 14 days of data the baseline is "forming"
    (spec 8.4) and rows render ``--`` in place of numbers.
    """
    store = ProfileStore()
    rows = store.load_session_scores()
    inputs = [
        BaselineInputSession(
            started_at=datetime.fromisoformat(row["started_at"]),
            overall=float(row["overall"]),
            dimension_scores=row["dimension_scores"],
            # Rates require re-parsing source files, which would
            # break the read-only contract of `baseline`. They are
            # omitted from this view; the weekly digest panel is the
            # canonical place to see them.
            engagement_rate=0.0,
            delegation_rate=0.0,
            independence_rate=0.0,
        )
        for row in rows
    ]
    as_of = datetime.now(timezone.utc)
    forming = is_baseline_forming(inputs, as_of=as_of)
    baseline = compute_baseline(inputs, as_of=as_of)

    print("\nPRAXIS - 90-DAY BASELINE\n")
    print(f"  Window:  {baseline.window_start} to {baseline.window_end}")
    print(f"  Sessions in window: {baseline.session_count}")
    if forming:
        print("  Baseline forming. Come back in 2 more weeks for week-over-week.")
        print(f"  Overall:           --")
        for d in RUBRIC:
            print(f"  {d.title.ljust(22)} --")
        return 0
    print(f"  Overall:           {baseline.overall_mean:.2f}/10")
    for d in RUBRIC:
        value = baseline.dimension_means.get(d.key, 0.0)
        print(f"  {d.title.ljust(22)} {value:.2f}/10")
    return 0


def cmd_history(args: argparse.Namespace) -> int:  # noqa: ARG001
    """List past weekly digests, newest first.

    Read-only: derives the list from the ISO weeks present in
    ``session_scores``. For each week it prints the count of judged
    sessions and the mean overall score. The user can then drill in
    with ``praxis show <week_iso>``.
    """
    weeks = list_persisted_weeks()
    if not weeks:
        print("No history yet. Run: praxis scan, then praxis week.")
        return 0
    print("\nPRAXIS - WEEKLY HISTORY\n")
    print(f"  {'Week'.ljust(12)} {'Sessions'.rjust(8)}   Overall")
    for entry in weeks:
        print(
            f"  {entry['week_iso'].ljust(12)} "
            f"{str(entry['session_count']).rjust(8)}   "
            f"{entry['overall_mean']:.2f}/10"
        )
    print(f"\nInspect one week: praxis show <week_iso>")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Render a past week's digest from persisted data.

    Read-only: equivalent to ``praxis week --week <iso>`` but skips the
    HTML/notify side-effect flags.

    Exit codes (spec 12.3):
      0  digest rendered.
      1  malformed ISO-week string.
      3  zero sessions persisted for that ISO week.
    """
    try:
        summary = run_weekly(week_iso=args.week_iso)
    except InvalidWeekError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if summary.snapshot.session_count == 0:
        print(no_sessions_message(summary.week_iso), file=sys.stderr)
        return 3
    print(render_terminal(summary))
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


def cmd_install_weekly(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Generate and load the macOS LaunchAgent for ``praxis week --notify``.

    On macOS, writes ``~/Library/LaunchAgents/co.praxis.weekly.plist``
    with the day/time from ``~/.praxis/config.toml`` and loads it via
    ``launchctl``. Re-running is idempotent: the existing job is
    unloaded first, the plist is overwritten, and the new job is
    loaded.

    Exit codes (spec 12.3):
      0  plist generated and loaded (macOS), or non-macOS placeholder
         path completed without scheduling anything.
      4  launchd installation failed (launchctl returned non-zero, or
         the config schedule has an invalid day/hour/minute).
    """
    if sys.platform != "darwin":
        # Non-macOS systemd/Task Scheduler output lands in US-083.
        # Until that story lands, exit cleanly so other platforms are
        # not blocked by this verb.
        print(
            "install-weekly: non-macOS scheduling is printed (not loaded). "
            "This branch is not yet implemented; coming in a follow-up story.",
            file=sys.stderr,
        )
        return 0
    from praxis.cli.install_weekly import (
        InstallWeeklyError,
        install_weekly_macos,
    )

    try:
        path = install_weekly_macos()
    except InstallWeeklyError as exc:
        print(f"install-weekly failed: {exc}", file=sys.stderr)
        return 4
    print(f"Installed weekly LaunchAgent: {path}")
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

    scan = sub.add_parser(
        "scan",
        help="Scan + score newly-discovered sessions (no digest render).",
        description=(
            "Discover new sessions and run the judge against them, "
            "persisting results into ~/.praxis/profile.db. Prints a "
            "one-line summary; the digest itself lives behind "
            "'praxis week' / 'praxis show <iso>'."
        ),
    )
    scan.add_argument("--since-days", type=int, default=30,
                      help="Only consider session files modified in the last N days.")
    scan.add_argument("--max-new", type=int, default=50,
                      help="Cap on newly-discovered sessions to deep-score per run.")
    scan.add_argument("--force-consolidate", action="store_true",
                      help="Force the consolidation step even if it ran today.")
    scan.set_defaults(func=cmd_scan)

    rescore = sub.add_parser(
        "re-score",
        help="Re-run the frontier judge for one session and update its row.",
        description=(
            "Look up the persisted session by stable_id, re-parse its "
            "source file, run the frontier judge, and overwrite the row "
            "in session_scores (and that session's moments). Useful when "
            "the judge prompt or model version changes."
        ),
    )
    rescore.add_argument(
        "session_stable_id",
        type=str,
        help="The stable_id of a session already in session_scores.",
    )
    rescore.set_defaults(func=cmd_re_score)

    base = sub.add_parser(
        "baseline",
        help="Print the current 90-day baseline (read-only).",
        description=(
            "Read-only summary of the 90-day rolling baseline that the "
            "weekly digest panel uses. Renders '--' when the user has "
            "less than 14 days of data (spec section 8.4)."
        ),
    )
    base.set_defaults(func=cmd_baseline)

    hist = sub.add_parser(
        "history",
        help="List past weekly digests, newest first (read-only).",
        description=(
            "Enumerate the ISO weeks present in session_scores with "
            "their session count and mean overall score. Drill into one "
            "with 'praxis show <week_iso>'."
        ),
    )
    hist.set_defaults(func=cmd_history)

    show = sub.add_parser(
        "show",
        help="Render a past week's digest from persisted data (read-only).",
        description=(
            "Render the persisted snapshot for the given ISO week "
            "(e.g. 2026-W21). No scanning, no scoring, no writes."
        ),
    )
    show.add_argument(
        "week_iso",
        type=str,
        metavar="WEEK_ISO",
        help="ISO-week tag of the week to render (e.g. 2026-W21).",
    )
    show.set_defaults(func=cmd_show)

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

    mod = sub.add_parser("models",
                         help="List model cards or show one in detail.")
    mod.add_argument("--show", type=str, default=None,
                     help="Show full details for one model card (by id or alias).")
    mod.set_defaults(func=cmd_models)

    iw = sub.add_parser(
        "install-weekly",
        help="Install the macOS LaunchAgent that runs 'praxis week --notify'.",
        description=(
            "Generate ~/Library/LaunchAgents/co.praxis.weekly.plist from "
            "the schedule in ~/.praxis/config.toml and load it via "
            "launchctl. Idempotent. On non-macOS this command prints the "
            "equivalent snippet without scheduling anything (spec 12.4)."
        ),
    )
    iw.set_defaults(func=cmd_install_weekly)

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
