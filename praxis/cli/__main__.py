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
import json
import os
import shutil
import subprocess
import sys
import traceback
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from praxis import __version__
from praxis.config import ensure_config_file
from praxis.follow_up import FollowUp
from praxis.orchestrator import (
    NO_API_KEY_MESSAGE,
    InvalidWeekError,
    ReScoreError,
    current_iso_week,
    has_api_key_configured,
    list_persisted_weeks,
    no_sessions_message,
    re_score_session,
    run,
    run_weekly,
)
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
    user can re-open past weeks; ``~/.praxis/latest.html`` is a symlink
    refreshed by ``_update_latest_symlink`` on each write.
    """
    weeks_dir = resolve_home() / "weeks"
    weeks_dir.mkdir(parents=True, exist_ok=True)
    return weeks_dir / f"{week_iso}.html"


def _update_latest_symlink(html_path: Path) -> None:
    """Refresh ~/.praxis/latest.html to point at the just-written digest.

    Spec section 13.1 / 13.2 ("the user clicks the notification, gets
    `~/.praxis/latest.html`"). Best-effort: if the filesystem doesn't
    support symlinks (some Windows configs) we just skip silently.
    """
    latest = resolve_home() / "latest.html"
    try:
        # Use relative target so the symlink remains valid if ~/.praxis
        # is moved or remounted.
        target = html_path.relative_to(resolve_home())
    except ValueError:
        target = html_path
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(target)
    except (OSError, NotImplementedError):
        pass


_TRAJECTORY_LABEL_DISPLAY: dict[str, str] = {
    "learning": "Learning",
    "stable_engaged": "Engaged",
    "stable_passive": "Passive",
    "atrophying": "Atrophying",
    "insufficient_data": "Reading",
}


_NOTIFY_TITLE = "Praxis weekly read is ready"
_NOTIFY_FAILURE_TITLE = "Praxis weekly digest failed"


def _applescript_quote(s: str) -> str:
    """Escape a string for safe inclusion inside an AppleScript string literal.

    AppleScript string literals only need backslash and double-quote
    escaped; newlines are passed through (display alert renders them).
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _post_notify_banner(
    title: str,
    body: str,
    sound: str,
    open_path: Path | None = None,  # noqa: ARG001
) -> None:
    """Non-interactive macOS banner via `osascript display notification`.

    `open_path` is accepted for signature parity with the alert /
    terminal-notifier helpers but ignored here: native banners are not
    clickable. The body is expected to tell the user the path.
    """
    if sys.platform != "darwin":
        return
    script = (
        f'display notification "{_applescript_quote(body)}" '
        f'with title "{_applescript_quote(title)}" '
        f'sound name "{_applescript_quote(sound)}"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"[cli] osascript notification failed: "
                f"exit {result.returncode}: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[cli] osascript notification failed: {exc!r}", file=sys.stderr)


def _post_notify_alert(
    title: str,
    body: str,
    sound: str,  # noqa: ARG001
    open_path: Path | None = None,
) -> None:
    """Modal AppleScript alert with an Open button.

    The user sees a dialog with two buttons -- Dismiss (default cancel)
    and Open (default action). Clicking Open invokes `open <path>` on
    `open_path` so the digest comes up in the default browser. When
    `open_path` is None or missing, only the Dismiss button is shown.

    Tradeoff: modal alerts interrupt the active window. That's exactly
    the point of opting in to this style -- the user wanted a notification
    that cannot be dismissed by accident.
    """
    if sys.platform != "darwin":
        return
    has_open = open_path is not None and Path(open_path).exists()
    if has_open:
        script = (
            f'set theResult to display alert '
            f'"{_applescript_quote(title)}" '
            f'message "{_applescript_quote(body)}" '
            f'buttons {{"Dismiss", "Open"}} '
            f'default button "Open" '
            f'cancel button "Dismiss"\n'
            f'if button returned of theResult is "Open" then\n'
            f'  do shell script "open " & '
            f'quoted form of "{_applescript_quote(str(open_path))}"\n'
            f'end if'
        )
    else:
        script = (
            f'display alert "{_applescript_quote(title)}" '
            f'message "{_applescript_quote(body)}" '
            f'buttons {{"Dismiss"}} default button "Dismiss"'
        )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        # `display alert` returns non-zero when the user closes via Cmd-.
        # (a "user cancelled" error). That isn't a failure -- swallow it.
        if result.returncode != 0 and "User canceled" not in (result.stderr or ""):
            print(
                f"[cli] osascript alert failed: "
                f"exit {result.returncode}: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[cli] osascript alert failed: {exc!r}", file=sys.stderr)


def _post_notify_terminal_notifier(
    title: str,
    body: str,
    sound: str,
    open_path: Path | None = None,
) -> None:
    """Click-to-open banner via the optional `terminal-notifier` binary.

    Falls back to `_post_notify_banner` (with a one-line stderr note)
    when the binary isn't on PATH. The `-open` flag carries a URL that
    `terminal-notifier` runs when the user clicks the banner; we pass
    a file:// URL pointing at `open_path` so the latest digest opens in
    the default browser.
    """
    if sys.platform != "darwin":
        return
    tn = shutil.which("terminal-notifier")
    if tn is None:
        print(
            "[cli] terminal-notifier not found on PATH; "
            "falling back to plain banner. Install via "
            "`brew install terminal-notifier` to enable click-to-open.",
            file=sys.stderr,
        )
        _post_notify_banner(title, body, sound, open_path)
        return
    argv = [tn, "-title", title, "-message", body, "-sound", sound]
    if open_path is not None and Path(open_path).exists():
        argv.extend(["-open", Path(open_path).as_uri()])
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"[cli] terminal-notifier failed: "
                f"exit {result.returncode}: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[cli] terminal-notifier failed: {exc!r}", file=sys.stderr)


_NOTIFY_STYLE_DISPATCH = {
    "banner": _post_notify_banner,
    "alert": _post_notify_alert,
    "terminal-notifier": _post_notify_terminal_notifier,
}


def _post_notify(
    trajectory_label: str | None = None,
    *,
    title: str | None = None,
    body: str | None = None,
    open_path: Path | None = None,
) -> None:
    """Best-effort macOS notification (spec section 13.2).

    Dispatches on `notification.style` from config.toml; falls back to
    `banner` for any unrecognised style. When called without overrides
    this preserves the legacy "weekly read is ready" framing.

    `open_path` defaults to `~/.praxis/latest.html` so the click-to-open
    styles (alert, terminal-notifier) actually have something to open.
    Callers (e.g. the failure path) override this to point at a log
    file instead.
    """
    if sys.platform != "darwin":
        return

    from praxis.config import load_config

    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        print(f"[cli] could not load config for notify: {exc!r}", file=sys.stderr)
        return

    if not cfg.notification.enabled:
        return

    resolved_title = title if title is not None else _NOTIFY_TITLE
    if body is None:
        if trajectory_label:
            resolved_body = (
                f"{trajectory_label} this week. "
                f"Open ~/.praxis/latest.html for the detail."
            )
        else:
            resolved_body = "Open ~/.praxis/latest.html to read."
    else:
        resolved_body = body
    resolved_path = (
        open_path if open_path is not None else _latest_html_path()
    )

    style = cfg.notification.style or "banner"
    fn = _NOTIFY_STYLE_DISPATCH.get(style, _post_notify_banner)
    fn(resolved_title, resolved_body, cfg.notification.sound, resolved_path)


def _weekly_error_log_path() -> Path:
    """Append-only log used by the failure-surfacing wrapper.

    Path mirrors the launchd plist's StandardErrorPath (`weekly.err.log`)
    so the user has one place to look when a notification says "failed".
    Created lazily; the parent dir is the same one install-weekly creates.
    """
    log_dir = resolve_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "weekly.err.log"


def _handle_week_failure(exc: BaseException) -> None:
    """Surface an unhandled `cmd_week --notify` crash.

    Two effects: append a timestamped traceback to
    `~/.praxis/logs/weekly.err.log`, and post a distinct failure
    notification (reusing whichever notification style is configured so
    the click-to-open helpers still work -- pointing at the log file
    instead of the digest, so clicking jumps straight to the diagnosis).
    """
    log_path = _weekly_error_log_path()
    stamp = datetime.now(timezone.utc).isoformat()
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    entry = f"\n[{stamp}] praxis week --notify failed\n{tb}\n"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(entry)
    except OSError as log_exc:
        print(
            f"[cli] could not append to {log_path}: {log_exc!r}",
            file=sys.stderr,
        )
    _post_notify(
        title=_NOTIFY_FAILURE_TITLE,
        body=f"Check {log_path}",
        open_path=log_path,
    )


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
                        When this flag is set the entire run is wrapped
                        in a failure handler that logs and notifies on
                        crash (exit 4) so launchd-fired runs never
                        silently disappear.
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
      4  unexpected exception during a --notify run; details in
         ``~/.praxis/logs/weekly.err.log``.
    """
    if args.notify:
        # The daemon path. Catch any unhandled exception so the run
        # produces SOMETHING the user can see -- a notification plus a
        # logged traceback -- instead of disappearing into launchd's
        # stderr. Direct (non-notify) invocations skip this wrap so
        # debugging stays Pythonic.
        try:
            return _cmd_week_impl(args)
        except SystemExit:
            raise
        except BaseException as exc:  # noqa: BLE001
            _handle_week_failure(exc)
            return 4
    return _cmd_week_impl(args)


def _cmd_week_impl(args: argparse.Namespace) -> int:
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

    # Loud-fail if the pipeline ran but the judge produced nothing.
    # This is the audit-#24 "silent degrade" case: ANTHROPIC/OPENAI key
    # was set, sessions were scanned, but every judge call errored. The
    # digest would technically render (with an empty headline moment
    # and forming dimensions) but the user should know the judge layer
    # failed rather than discover it by an unexpectedly blank report.
    is_current_week = summary.week_iso is None or (
        summary.week_iso == current_iso_week()
    )
    judged_count = len(summary.judge_results) if summary.judge_results else 0
    if (
        is_current_week
        and not args.dry_run
        and summary.sessions
        and judged_count == 0
    ):
        print(
            "WARNING: scanned sessions but the judge produced no scores. "
            "Check provider keys / billing (see prior stderr lines). "
            "The digest below reflects what's persisted, not this run's work.",
            file=sys.stderr,
        )

    print(summary.rendered_terminal)

    target_week = summary.week_iso or "current"
    if args.write_html or args.notify:
        html_path = _weekly_html_path(target_week)
        html_path.write_text(summary.rendered_html, encoding="utf-8")
        _update_latest_symlink(html_path)
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
        traj_label: str | None = None
        if summary.trajectory is not None:
            raw = summary.trajectory.label.value
            traj_label = _TRAJECTORY_LABEL_DISPLAY.get(raw, raw.title())
        _post_notify(trajectory_label=traj_label)

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
    )

    print(
        f"Scanned {summary.sessions_seen} session(s); "
        f"{summary.sessions_new} new; "
        f"scored {summary.sessions_scored} via judge "
        f"({summary.elapsed_seconds}s)."
    )
    print("Render the digest with: praxis week")
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
        print("  Overall:           --")
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
    print(f"  {'Week'.ljust(12)} {'Sessions'.rjust(8)}   Overall  HTML")
    weeks_dir = resolve_home() / "weeks"
    for entry in weeks:
        # Surface the per-week HTML path when it exists on disk so the
        # listing is actionable (the user can `open` it directly).
        html_marker = ""
        if weeks_dir.exists():
            candidate = weeks_dir / f"{entry['week_iso']}.html"
            html_marker = str(candidate) if candidate.exists() else ""
        print(
            f"  {entry['week_iso'].ljust(12)} "
            f"{str(entry['session_count']).rjust(8)}   "
            f"{entry['overall_mean']:.2f}/10  "
            f"{html_marker}"
        )
    print("\nInspect one week: praxis show <week_iso>")
    print("Open this week:   praxis open")
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
    print(summary.rendered_terminal)
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
        print("Note: `praxis report` opens the legacy v0.1 report; "
              "for the weekly digest use `praxis open`.")
    return 0


def _latest_html_path() -> Path:
    """Resolve the `~/.praxis/latest.html` symlink.

    Always returns the *target* path the symlink points at when one is on
    disk; falls back to the literal `~/.praxis/latest.html` path so the
    `.exists()` check tells the caller whether anything is there to open.
    """
    latest = resolve_home() / "latest.html"
    if latest.is_symlink():
        target = Path(os.readlink(latest))
        if not target.is_absolute():
            target = (latest.parent / target).resolve()
        return target
    return latest


def _touch_last_opened() -> None:
    """Record that the user opened the latest digest.

    The shell-startup nudge compares this marker's mtime against
    `latest.html`'s mtime to decide whether to print a reminder. We
    write a single ISO timestamp into the file rather than just
    touch()ing it so a casual `cat .last_opened` is human-readable.
    """
    marker = resolve_home() / ".last_opened"
    try:
        marker.write_text(
            datetime.now(timezone.utc).isoformat() + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[cli] could not write {marker}: {exc!r}", file=sys.stderr)


def cmd_open(args: argparse.Namespace) -> int:
    """Open this week's digest in the default browser.

    Resolves `~/.praxis/latest.html` (symlink maintained by the daemon),
    opens it via `webbrowser.open`, and touches `~/.praxis/.last_opened`
    so the shell-startup nudge stops nagging. `--print` writes the HTML
    to stdout instead of opening a window.

    Exit codes:
      0  digest opened (or printed).
      1  no digest on disk yet.
    """
    html = _latest_html_path()
    if not html.exists():
        print(
            "No weekly digest yet. Run `praxis week --write-html` or "
            "install the daemon with `praxis install-weekly`.",
            file=sys.stderr,
        )
        return 1
    if args.print:
        print(html.read_text(encoding="utf-8"))
    else:
        webbrowser.open(html.as_uri())
        print(f"Opened: {html}")
    _touch_last_opened()
    return 0


def cmd_last(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Print metadata about the latest digest without opening it.

    Useful in scripts (`open "$(praxis last --path-only)"`) and as a
    no-window sanity check that the daemon ran. Prints: path, ISO week,
    and (when available) the trajectory label.
    """
    html = _latest_html_path()
    if not html.exists():
        print(
            "No weekly digest yet. Run `praxis week --write-html` or "
            "install the daemon with `praxis install-weekly`.",
            file=sys.stderr,
        )
        return 1
    if getattr(args, "path_only", False):
        print(html)
        return 0
    # The week_iso lives in the filename: weeks/<iso>.html.
    week_iso = html.stem
    print(f"Week:  {week_iso}")
    print(f"Path:  {html}")
    # Best-effort trajectory label from the persisted weekly_digests row.
    try:
        store = ProfileStore()
        row = store.load_weekly_digest(week_iso)
        if row and row.get("trajectory_label"):
            label = _TRAJECTORY_LABEL_DISPLAY.get(
                row["trajectory_label"], row["trajectory_label"].title()
            )
            print(f"Label: {label}")
    except Exception as exc:  # noqa: BLE001
        # Read-only metadata fetch; never block the user on a DB issue.
        print(f"[cli] could not read trajectory label: {exc!r}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    store = ProfileStore()
    rows = store.load_session_scores()
    digest_count = store.count_weekly_digests()
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
    print(f"Weekly digests on file: {digest_count}")
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


def _resolve_active_commitment(week_iso: str) -> FollowUp | None:
    """Return the single active commitment for ``week_iso``, or None.

    Raises ``RuntimeError`` when more than one active row exists -- that's
    the "would only happen if the unique index was bypassed" case in the
    spec (US-016 AC #3). Surfacing it loud is the whole point: silently
    picking one would mask the corrupted invariant.
    """
    store = ProfileStore()
    commitments = store.load_active_commitments(week_iso)
    if len(commitments) > 1:
        raise RuntimeError(
            f"praxis nudge: {len(commitments)} active commitments found for "
            f"{week_iso}; expected at most one (active = outcome='pending' "
            "AND superseded_by IS NULL). The follow_ups unique-index "
            "invariant has been violated."
        )
    return commitments[0] if commitments else None


def cmd_nudge(args: argparse.Namespace) -> int:
    """Print this week's active commitment, or stay silent.

    Resolves the single follow_ups row for the current ISO week that has
    ``outcome='pending'`` (and, once the schema-migrations columns land,
    ``superseded_by IS NULL``). The ``--format`` flag selects the surface:

      text         (default) ``[Praxis] This week: <commitment>`` + newline,
                   for human-readable shell / terminal surfaces.
      claude-code  ``{"hookSpecificOutput":{"additionalContext":"[Praxis] ``
                   ``This week's focus: <commitment>"}}`` (single-line JSON,
                   for Claude Code SessionStart hooks per spec section 5).
      codex        Same JSON shape as claude-code (Codex SessionStart hooks
                   share the additionalContext envelope per spec section 5).

    Exit codes:
      0  active commitment printed, or no active commitment (silent).
      4  invariant violated: more than one active row for the current week.
    """
    week_iso = current_iso_week()
    try:
        active = _resolve_active_commitment(week_iso)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    if active is None:
        return 0
    display_text = active.commitment_text
    fmt = getattr(args, "format", "text")
    if fmt == "text":
        print(f"[Praxis] This week: {display_text}")
    else:
        payload = {
            "hookSpecificOutput": {
                "additionalContext": f"[Praxis] This week's focus: {display_text}",
            },
        }
        print(json.dumps(payload, separators=(",", ":")))
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

    On non-macOS, no scheduling is attempted. Instead, the equivalent
    systemd user timer (Linux) or Task Scheduler XML (Windows) is
    printed to stdout AND saved to
    ``~/.praxis/install-weekly-snippet.txt`` so the user can install it
    themselves (spec 12.4).

    Exit codes (spec 12.3):
      0  plist generated and loaded (macOS), or snippet printed/saved
         (non-macOS).
      4  launchd installation failed (launchctl returned non-zero, or
         the config schedule has an invalid day/hour/minute).
    """
    if sys.platform != "darwin":
        from praxis.cli.install_weekly import (
            InstallWeeklyError as _InstallWeeklyError,
            write_non_macos_snippet,
        )
        try:
            path, content = write_non_macos_snippet()
        except _InstallWeeklyError as exc:
            print(f"install-weekly failed: {exc}", file=sys.stderr)
            return 4
        print(content)
        print(f"Saved snippet to: {path}")
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

    # Offer the shell-startup reminder. The notification UX is best-
    # effort -- the user can miss the banner / dismiss it / be in Focus
    # mode -- so a once-per-shell reminder closes the surfacing gap.
    _maybe_prompt_shell_nudge(args)
    return 0


def _maybe_prompt_shell_nudge(args: argparse.Namespace) -> None:
    """Optionally install the shell-startup reminder.

    Decision tree:
      --no-shell-nudge       skip entirely.
      --yes                  install without prompting.
      no TTY (e.g. piped CI) skip entirely (default = don't).
      otherwise              prompt; default Yes.
    """
    from praxis.cli.shell_nudge import install_into_all, rc_candidates

    no_nudge = getattr(args, "no_shell_nudge", False)
    assume_yes = getattr(args, "yes", False)

    if no_nudge:
        return
    if not assume_yes:
        if not sys.stdin.isatty():
            # Non-interactive shell: don't surprise scripts with edits
            # to ~/.zshrc. The user can run `praxis install-shell-nudge`
            # later if they want it.
            return
        try:
            reply = input(
                "Add a shell-startup reminder so you don't miss the digest? [Y/n] "
            ).strip().lower()
        except EOFError:
            return
        if reply not in {"", "y", "yes"}:
            print(
                "Skipped. You can install it later with "
                "`praxis install-shell-nudge`."
            )
            return

    modified = install_into_all()
    if not modified:
        existing = [p for p in rc_candidates() if p.exists()]
        if existing:
            print(
                "Shell-startup reminder already present in: "
                + ", ".join(str(p) for p in existing)
            )
        else:
            print(
                "No ~/.zshrc or ~/.bashrc found; shell-startup reminder "
                "not installed. Create one of those files and re-run "
                "`praxis install-shell-nudge`."
            )
        return
    for p in modified:
        print(f"Installed shell-startup reminder in: {p}")
    print("Open a new shell to start receiving the reminder.")


def cmd_shell_nudge(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Print the shell snippet that `eval "$(praxis shell-nudge)"` consumes.

    The snippet is pure shell (no Python invoked per shell startup) and
    is safe to eval in both zsh and bash. When `latest.html` is fresh
    and `.last_opened` is missing/stale, one line is printed to stderr
    on shell startup.
    """
    from praxis.cli.shell_nudge import emit_snippet
    print(emit_snippet(), end="")
    return 0


def cmd_install_shell_nudge(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Append the shell-nudge eval line to ~/.zshrc and ~/.bashrc.

    Idempotent: re-running is a no-op when the line is already there.
    Reports which file(s) were touched.
    """
    from praxis.cli.shell_nudge import install_into_all, rc_candidates

    modified = install_into_all()
    if modified:
        for p in modified:
            print(f"Installed shell-startup reminder in: {p}")
        print("Open a new shell to start receiving the reminder.")
        return 0
    existing = [p for p in rc_candidates() if p.exists()]
    if existing:
        print(
            "Shell-startup reminder already present in: "
            + ", ".join(str(p) for p in existing)
        )
        return 0
    print(
        "No ~/.zshrc or ~/.bashrc found. Create one and re-run this command.",
        file=sys.stderr,
    )
    return 1


def cmd_uninstall_shell_nudge(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Remove the shell-nudge eval line (and its comment marker) from RC files."""
    from praxis.cli.shell_nudge import uninstall_from_all

    modified = uninstall_from_all()
    if modified:
        for p in modified:
            print(f"Removed shell-startup reminder from: {p}")
    else:
        print("No shell-startup reminder found to remove.")
    return 0


def cmd_uninstall_weekly(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Unload and delete the macOS LaunchAgent installed by ``install-weekly``.

    On macOS, runs ``launchctl unload`` on
    ``~/Library/LaunchAgents/co.praxis.weekly.plist`` (best-effort) and
    then deletes the plist file. Running this when no job is installed
    is not an error: per AC US-080, the user contract is "after
    uninstall-weekly, the weekly job is not scheduled," which is
    trivially satisfied when nothing was scheduled to begin with.

    Exit codes:
      0  always (job removed, or never installed).
    """
    if sys.platform != "darwin":
        # Non-macOS is a no-op symmetric with install-weekly's
        # placeholder branch -- there is nothing to remove because
        # nothing was scheduled.
        print(
            "uninstall-weekly: non-macOS has no scheduled job to remove.",
            file=sys.stderr,
        )
        return 0
    from praxis.cli.install_weekly import (
        plist_path,
        uninstall_weekly_macos,
    )

    removed = uninstall_weekly_macos()
    path = plist_path()
    if removed:
        print(f"Removed weekly LaunchAgent: {path}")
    else:
        print(f"No weekly LaunchAgent found at: {path}")
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
    print("Inspect one card: praxis models --show <id>")
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

    rep = sub.add_parser("report", help="Open or print the legacy v0.1 HTML report.")
    rep.add_argument("--print", action="store_true",
                     help="Print HTML to stdout instead of opening browser.")
    rep.set_defaults(func=cmd_report)

    opn = sub.add_parser(
        "open",
        help="Open this week's digest in the default browser.",
        description=(
            "Open ~/.praxis/latest.html (the symlink the daemon updates "
            "on every weekly run). Touches ~/.praxis/.last_opened so the "
            "shell-startup reminder stops nagging once read."
        ),
    )
    opn.add_argument("--print", action="store_true",
                     help="Print HTML to stdout instead of opening a window.")
    opn.set_defaults(func=cmd_open)

    lst = sub.add_parser(
        "last",
        help="Print the latest digest's path, ISO week, and trajectory label.",
        description=(
            "Read-only metadata about ~/.praxis/latest.html. Does not "
            "open a browser window. Use --path-only for scripting "
            "(e.g. `open \"$(praxis last --path-only)\"`)."
        ),
    )
    lst.add_argument("--path-only", action="store_true",
                     help="Print only the absolute path (one line, no labels).")
    lst.set_defaults(func=cmd_last)

    sts = sub.add_parser("status", help="Show current scorecard status.")
    sts.set_defaults(func=cmd_status)

    rub = sub.add_parser("rubric", help="Print the scoring rubric.")
    rub.set_defaults(func=cmd_rubric)

    fup = sub.add_parser(
        "follow-up",
        help="Show the most recent weekly commitment and its outcome.",
    )
    fup.set_defaults(func=cmd_follow_up)

    nudge = sub.add_parser(
        "nudge",
        help="Print this week's active commitment (silent when none exists).",
        description=(
            "Resolve the single follow_ups row with outcome='pending' for the "
            "current ISO week and print it on one line. Exits 0 with empty "
            "stdout when there is no active commitment so hooks (Claude Code "
            "SessionStart, Codex, shell startup) stay silent until the first "
            "commitment is recorded."
        ),
    )
    nudge.add_argument(
        "--format",
        choices=["text", "claude-code", "codex"],
        default="text",
        help=(
            "Output format. 'text' (default) is a single human-readable line. "
            "'claude-code' and 'codex' emit a single-line JSON envelope "
            "({\"hookSpecificOutput\":{\"additionalContext\":...}}) for "
            "SessionStart hooks per spec section 5."
        ),
    )
    nudge.set_defaults(func=cmd_nudge)

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
    iw.add_argument(
        "--no-shell-nudge", action="store_true",
        help="Skip the shell-startup reminder prompt entirely.",
    )
    iw.add_argument(
        "--yes", action="store_true",
        help="Assume yes for the shell-startup reminder prompt "
             "(useful in scripted installs).",
    )
    iw.set_defaults(func=cmd_install_weekly)

    uw = sub.add_parser(
        "uninstall-weekly",
        help="Unload and remove the macOS LaunchAgent installed by install-weekly.",
        description=(
            "Run 'launchctl unload' against "
            "~/Library/LaunchAgents/co.praxis.weekly.plist and delete the "
            "file. Exits 0 even when no job is currently installed."
        ),
    )
    uw.set_defaults(func=cmd_uninstall_weekly)

    sn = sub.add_parser(
        "shell-nudge",
        help="Print the shell snippet for `eval` in ~/.zshrc / ~/.bashrc.",
        description=(
            "Emit a tiny shell function that prints one reminder line "
            "when ~/.praxis/latest.html is fresh and unread. Designed "
            "to be wired in via `eval \"$(praxis shell-nudge)\"` -- the "
            "install-shell-nudge command does that for you."
        ),
    )
    sn.set_defaults(func=cmd_shell_nudge)

    isn = sub.add_parser(
        "install-shell-nudge",
        help="Append the shell-nudge eval line to ~/.zshrc and ~/.bashrc.",
        description=(
            "Idempotent. Adds `eval \"$(praxis shell-nudge)\"` to every "
            "existing RC file (zsh and/or bash). Skips files that "
            "already have the line."
        ),
    )
    isn.set_defaults(func=cmd_install_shell_nudge)

    usn = sub.add_parser(
        "uninstall-shell-nudge",
        help="Remove the shell-nudge eval line from ~/.zshrc and ~/.bashrc.",
    )
    usn.set_defaults(func=cmd_uninstall_shell_nudge)

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
