"""CLI entry point.

Surface organisation (US-044):
  Loop verbs (Commit -> Cue -> Reflect -> Review, plus install-coach) are
  listed first in `praxis --help`; everything else lives under a "More"
  heading.

Active commands:
  review           Render this week's digest (the Review step of the loop;
                   was named `week` in v0.2 and renamed in US-033/US-044).
  scan             Scan + score new sessions; no digest rendered.
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
import select
import shutil
import subprocess
import sys
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from praxis import __version__
from praxis.cli.nudge_throttle import is_throttled, record_fire
from praxis.config import ReflectConfig, ensure_config_file, load_config
from praxis.follow_up import FollowUp
from praxis.orchestrator import (
    NO_API_KEY_MESSAGE,
    InvalidWeekError,
    ReScoreError,
    current_iso_week,
    has_api_key_configured,
    list_persisted_weeks,
    no_sessions_message,
    parse_iso_week,
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
from praxis.storage.profile_store import (
    ActiveCommitment,
    MultipleActiveCommitmentsError,
    ProfileStore,
    SelfReport,
    resolve_home,
)


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


def _handle_review_failure(exc: BaseException) -> None:
    """Surface an unhandled `cmd_review --notify` crash.

    Two effects: append a timestamped traceback to
    `~/.praxis/logs/weekly.err.log`, and post a distinct failure
    notification (reusing whichever notification style is configured so
    the click-to-open helpers still work -- pointing at the log file
    instead of the digest, so clicking jumps straight to the diagnosis).
    """
    log_path = _weekly_error_log_path()
    stamp = datetime.now(timezone.utc).isoformat()
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    entry = f"\n[{stamp}] praxis review --notify failed\n{tb}\n"
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


# Spec section 2 (coaching-reposition): the masthead's follow-up prompt
# offers three choices. The single-letter responses are the contract the
# test suite asserts on, so a copy change here must update the prompt
# test in tests/test_cli.py.
_FOLLOWUP_PROMPT_TEXT = (
    "Choose:  [k]eep this commitment / [n]ew commitment / [d]igest only > "
)
_FOLLOWUP_CHOICE_KEEP = "k"
_FOLLOWUP_CHOICE_NEW = "n"
_FOLLOWUP_CHOICE_DIGEST = "d"
_VALID_FOLLOWUP_CHOICES = (
    _FOLLOWUP_CHOICE_KEEP,
    _FOLLOWUP_CHOICE_NEW,
    _FOLLOWUP_CHOICE_DIGEST,
)


def _next_iso_week(week_iso: str) -> str:
    """Return the ISO-week tag for the week immediately after ``week_iso``.

    Used by the [k]eep / [n]ew prompt paths so a chosen commitment lands
    in the next week's row regardless of which week was rendered. Raises
    ``InvalidWeekError`` for malformed input, mirroring the orchestrator's
    contract on its sibling ``_prior_iso_week`` helper.
    """
    week_start, _ = parse_iso_week(week_iso)
    nxt = week_start + timedelta(days=7)
    year, week, _ = nxt.isocalendar()
    return f"{year:04d}-W{week:02d}"


def _should_prompt_for_followup_choice(args: argparse.Namespace) -> bool:
    """Gate the [k]/[n]/[d] prompt on stdin TTY + the opt-out flags.

    Per spec section 2 / US-036 AC #2: the prompt only renders when the
    user is at an interactive terminal AND the run was not invoked
    through the scheduled daemon path (--notify) AND the user did not
    explicitly opt out (--non-interactive). The three predicates compose
    so the launchd-fired `praxis review --notify --non-interactive` path
    never blocks waiting on input.
    """
    if getattr(args, "notify", False):
        return False
    if getattr(args, "non_interactive", False):
        return False
    if not sys.stdin.isatty():
        return False
    return True


def _read_followup_choice() -> str:
    """Prompt the user for [k]/[n]/[d] and return the normalized choice.

    Repeatedly reads from stdin until the user enters one of the three
    valid letters (case-insensitive). EOF (Ctrl-D) is treated as 'd'
    (digest only) so a pipe-closed shell does not hang the run.
    """
    while True:
        try:
            raw = input(_FOLLOWUP_PROMPT_TEXT)
        except EOFError:
            return _FOLLOWUP_CHOICE_DIGEST
        choice = raw.strip().lower()[:1]
        if choice in _VALID_FOLLOWUP_CHOICES:
            return choice
        print("  Please enter k, n, or d.")


def _keep_commitment_for_next_week(
    store: ProfileStore, current: FollowUp
) -> str:
    """Insert a follow_ups row for next week carrying the same commitment.

    The new row mirrors the current commitment's dim_key, target_metric,
    and baseline_value so next week's review can close the loop on the
    same metric. measured_value/outcome reset to None/'pending' since the
    new week has not been measured yet. Returns the new row's week_iso so
    the caller can confirm the dispatch.
    """
    next_week = _next_iso_week(current.week_iso)
    new_row = FollowUp(
        week_iso=next_week,
        dim_key=current.dim_key,
        commitment_text=current.commitment_text,
        target_metric=current.target_metric,
        baseline_value=current.baseline_value,
        measured_value=None,
        outcome="pending",
    )
    store.save_follow_up(new_row)
    return next_week


# NOTE: a simpler `cmd_commit` (interactive prompt → save row for next
# week) was defined here by ralph/factory/praxis-review-rename-and-masthead
# (US-036). The canonical `cmd_commit` defined further below
# (ralph/factory/praxis-commit-command, US-020..023) is a superset of
# that behavior (suggestion sources, free-text 280-cap, supersede flow)
# so the inline simpler version was removed at merge time. Python's
# late binding means `_handle_followup_prompt`'s `[n]ew` branch resolves
# to the canonical implementation automatically.


def _handle_followup_prompt(summary_week_iso: str | None) -> None:
    """Render the masthead's follow-up prompt and dispatch on the answer.

    Caller has already confirmed the prompt should render (see
    ``_should_prompt_for_followup_choice``) and that the rendered digest
    carried a commitment rollup, so a follow_up row for the targeted
    week must exist. The dispatch is a side effect only: the function
    returns nothing because the digest has already been printed and the
    review verb's exit code is decided in ``_cmd_review_impl``.
    """
    store = ProfileStore()
    target_week = summary_week_iso or current_iso_week()
    current = store.load_follow_up(target_week)
    if current is None:
        # Defensive: rollup was present at render time but the row
        # disappeared between then and now. Skip the prompt rather than
        # raise; the user can still re-run.
        return
    choice = _read_followup_choice()
    if choice == _FOLLOWUP_CHOICE_DIGEST:
        return
    if choice == _FOLLOWUP_CHOICE_KEEP:
        next_week = _keep_commitment_for_next_week(store, current)
        print(f"  Kept commitment for {next_week}.")
        return
    if choice == _FOLLOWUP_CHOICE_NEW:
        # Canonical cmd_commit (US-020..023) ignores its args argument
        # but is typed as Namespace; pass an empty Namespace so the
        # inline-from-review dispatch type-checks cleanly.
        cmd_commit(argparse.Namespace())


def cmd_review(args: argparse.Namespace) -> int:
    """Render this week's digest (or a past week with --week <iso>).

    `review` is the Review step of the Commit -> Cue -> Reflect -> Review
    loop documented in the README masthead. It was named `week` in v0.2;
    the rename landed in US-033/US-044 alongside the help reorganisation
    so the CLI vocabulary matches the coaching narrative.

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
            return _cmd_review_impl(args)
        except SystemExit:
            raise
        except BaseException as exc:  # noqa: BLE001
            _handle_review_failure(exc)
            return 4
    return _cmd_review_impl(args)


def _cmd_review_impl(args: argparse.Namespace) -> int:
    needs_judge = args.week is None and not args.dry_run
    if needs_judge and not has_api_key_configured():
        print(NO_API_KEY_MESSAGE, file=sys.stderr)
        return 2

    # Issue #5: --max-new accepts 0 (or any non-positive) as "unbounded";
    # run_weekly distinguishes None / 0 from a positive cap internally.
    raw_max_new = getattr(args, "max_new", 50)
    max_new_arg: int | None = raw_max_new if raw_max_new and raw_max_new > 0 else None
    from praxis.storage.lock import ScanLockError, scan_lock
    try:
        with scan_lock(resolve_home()):
            summary = run_weekly(
                week_iso=args.week,
                dry_run=args.dry_run,
                frontier_only=args.frontier_only,
                explain_judging=args.explain_judging,
                max_new=max_new_arg,
            )
    except ScanLockError as exc:
        print(f"praxis review: {exc}. Try again in a moment.", file=sys.stderr)
        return 1
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
        and args.week is None
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

    # Spec section 2 (coaching-reposition): after the terminal masthead
    # prints, the interactive review offers the [k]eep / [n]ew / [d]igest
    # choice. The prompt only fires when an active commitment exists
    # (rollup is not None) AND the caller is at an interactive TTY AND
    # neither --notify nor --non-interactive was passed. The launchd
    # daemon path satisfies the --notify suppression; scripted runs that
    # want the rendered output without the prompt pass --non-interactive.
    if (
        summary.commitment_rollup is not None
        and _should_prompt_for_followup_choice(args)
    ):
        _handle_followup_prompt(summary.week_iso)

    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan source files and score newly-discovered sessions.

    Per spec 12.1 the ``scan`` verb is intentionally NOT a digest
    renderer: it does the work of discovering new sessions and persisting
    judge results, and prints a one-line summary of what changed. The
    digest (terminal masthead, dimensions, coaching, trajectory) is the
    job of ``praxis review`` and ``praxis show <week_iso>``.

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

    from praxis.storage.lock import ScanLockError, scan_lock
    try:
        with scan_lock(resolve_home()):
            summary = run(
                since_days=args.since_days,
                max_new_scored=args.max_new,
            )
    except ScanLockError as exc:
        print(f"praxis scan: {exc}. Try again in a moment.", file=sys.stderr)
        return 1

    skipped = getattr(summary, "sessions_skipped", 0)
    skipped_note = f"; skipped {skipped} (errors, see log)" if skipped else ""
    print(
        f"Scanned {summary.sessions_seen} session(s); "
        f"{summary.sessions_new} new; "
        f"scored {summary.sessions_scored} via judge{skipped_note} "
        f"({summary.elapsed_seconds}s)."
    )
    print("Render the digest with: praxis review")
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
        print("No history yet. Run: praxis scan, then praxis review.")
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

    Read-only: equivalent to ``praxis review --week <iso>`` but skips the
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
            "No weekly digest yet. Run `praxis review --write-html` or "
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
            "No weekly digest yet. Run `praxis review --write-html` or "
            "install the daemon with `praxis install-weekly`.",
            file=sys.stderr,
        )
        return 1
    if getattr(args, "path_only", False):
        print(html)
        return 0
    # The week_iso lives in the filename: weeks/<iso>.html.
    week_iso = html.stem
    raw_label = None
    label = None
    # Best-effort trajectory label from the persisted weekly_digests row.
    try:
        store = ProfileStore()
        row = store.load_weekly_digest(week_iso)
        if row and row.get("trajectory_label"):
            raw_label = row["trajectory_label"]
            label = _TRAJECTORY_LABEL_DISPLAY.get(raw_label, raw_label.title())
    except Exception as exc:  # noqa: BLE001
        # Read-only metadata fetch; never block the user on a DB issue.
        print(f"[cli] could not read trajectory label: {exc!r}", file=sys.stderr)
    if getattr(args, "json", False):
        print(json.dumps(
            {"week": week_iso, "path": str(html), "label": label,
             "trajectory": raw_label}, indent=2))
        return 0
    print(f"Week:  {week_iso}")
    print(f"Path:  {html}")
    if label:
        print(f"Label: {label}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Check the install end to end and print what's healthy vs. what to fix.

    Covers API keys, the profile database (presence + integrity + scored
    count + this week's focus), the per-tool coaching hooks, and the weekly
    schedule. Exits 0 unless something critical (an unreadable database) is
    wrong, so it's safe to run in support scripts.
    """
    import sqlite3

    home = resolve_home()
    critical_ok = True
    issues: list[str] = []

    def mark(passed: bool) -> str:
        return "✓" if passed else "✗"

    print(f"Praxis doctor  -  home: {home}\n")

    # --- API keys ---
    anth = bool(os.environ.get("ANTHROPIC_API_KEY"))
    openai = bool(os.environ.get("OPENAI_API_KEY"))
    print("API keys")
    print(f"  [{mark(anth)}] ANTHROPIC_API_KEY")
    print(f"  [{mark(openai)}] OPENAI_API_KEY")
    if not (anth or openai):
        issues.append("No API key set - scoring runs heuristics-only. "
                      "export ANTHROPIC_API_KEY=... or OPENAI_API_KEY=...")

    # --- database ---
    db = home / "profile.db"
    print("\nDatabase")
    if not db.exists():
        print(f"  [{mark(False)}] profile.db not found (it's created on first run)")
    else:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            scored = con.execute("SELECT COUNT(*) FROM session_scores").fetchone()[0]
            digests = con.execute("SELECT COUNT(*) FROM weekly_digests").fetchone()[0]
            con.close()
            ok = integrity == "ok"
            print(f"  [{mark(ok)}] integrity_check: {integrity}")
            print(f"  [{mark(True)}] {scored} sessions scored, {digests} weekly digests")
            if not ok:
                critical_ok = False
                issues.append("Database failed its integrity check; a backup "
                              "may sit beside it (profile.db.backup-*).")
        except Exception as exc:  # noqa: BLE001
            critical_ok = False
            print(f"  [{mark(False)}] could not read profile.db: {exc}")
            issues.append("Database is unreadable. Restore from a "
                          "profile.db.backup-* sibling if one exists.")

    # --- this week's focus ---
    try:
        active = ProfileStore().active_follow_up_for_week(current_iso_week())
        print("\nThis week")
        if active is not None:
            print(f"  [{mark(True)}] focus set: "
                  f"{(active.display_text or active.commitment_text)[:60]}")
        else:
            print(f"  [{mark(False)}] no focus set")
            issues.append("No focus this week - run `praxis commit`.")
    except Exception:  # noqa: BLE001
        pass

    # --- coaching hooks ---
    from praxis.cli.install_coach import (
        claude_settings_path,
        codex_hooks_path,
    )

    def _contains(path, needle: str) -> bool:
        try:
            return path.exists() and needle in path.read_text(encoding="utf-8")
        except OSError:
            return False

    claude_ok = _contains(claude_settings_path(), "_praxisManaged")
    codex_ok = _contains(codex_hooks_path(), "praxis nudge")
    print("\nCoaching hooks")
    print(f"  [{mark(claude_ok)}] Claude Code")
    print(f"  [{mark(codex_ok)}] Codex")
    if not (claude_ok or codex_ok):
        issues.append("No coaching hooks installed - run `praxis install-coach`.")

    # --- weekly schedule (macOS) ---
    if sys.platform == "darwin":
        from praxis.cli.install_weekly import plist_path
        sched = plist_path().exists()
        print("\nWeekly digest schedule")
        print(f"  [{mark(sched)}] LaunchAgent installed")
        if not sched:
            issues.append("Weekly digest not scheduled - run `praxis install-weekly`.")

    # --- summary ---
    if issues:
        print(f"\n{len(issues)} thing(s) to look at:")
        for i in issues:
            print(f"  - {i}")
    else:
        print("\nEverything looks healthy.")
    return 0 if critical_ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    store = ProfileStore()
    rows = store.load_session_scores()
    digest_count = store.count_weekly_digests()
    providers: dict[str, int] = {}
    for r in rows:
        providers[r["provider"]] = providers.get(r["provider"], 0) + 1
    if getattr(args, "json", False):
        print(json.dumps({
            "home": str(resolve_home()),
            "sessions_scored": len(rows),
            "first": rows[0]["started_at"] if rows else None,
            "most_recent": rows[-1]["started_at"] if rows else None,
            "providers": providers,
            "weekly_digests": digest_count,
        }, indent=2))
        return 0
    print(f"Scorecard home: {resolve_home()}")
    print(f"Sessions scored: {len(rows)}")
    if rows:
        print(f"  First: {rows[0]['started_at']}")
        print(f"  Most recent: {rows[-1]['started_at']}")
        for prov, count in sorted(providers.items(), key=lambda kv: -kv[1]):
            print(f"  {prov}: {count}")
    print(f"Weekly digests on file: {digest_count}")
    return 0


def _commit_non_interactive(store, week_iso, pick, free_text) -> int:
    """Write a commitment without prompting (praxis commit --pick N / --text).

    Reuses the exact engine path the interactive flow and the menu-bar app use:
    build_user_chosen_follow_up + supersede-or-insert. Returns a CLI exit code.
    """
    import sqlite3

    from praxis.cli.commit import (
        CommitSuggestion,
        build_commit_suggestions,
        build_user_chosen_follow_up,
        load_commit_context,
    )

    suggestions = build_commit_suggestions(load_commit_context(store, week_iso=week_iso))
    if free_text is not None:
        text = free_text.strip()
        if not text:
            print("praxis commit: --text was empty.", file=sys.stderr)
            return 1
        suggestion = next(
            (s for s in suggestions if s.kind == "free_text"), None
        ) or CommitSuggestion(kind="free_text", text="Write your own")
        display = text[:280]
    else:
        picks = [s for s in suggestions if s.kind in ("headline", "drill")]
        if not picks:
            print("praxis commit: no suggestions available; run `praxis scan` first.",
                  file=sys.stderr)
            return 1
        if pick < 1 or pick > len(picks):
            print(f"praxis commit: --pick must be between 1 and {len(picks)}.",
                  file=sys.stderr)
            return 1
        suggestion = picks[pick - 1]
        display = suggestion.text

    follow_up = build_user_chosen_follow_up(
        week_iso=week_iso, suggestion=suggestion, display_text=display,
        prior=store.latest_follow_up())
    prior_id: int | None = None
    if store.active_follow_up_for_week(week_iso) is not None:
        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM follow_ups WHERE week_iso=? AND outcome='pending' "
                "AND superseded_by IS NULL LIMIT 1", (week_iso,)).fetchone()
        prior_id = int(row[0]) if row else None
    try:
        if prior_id is not None:
            store.supersede_and_insert_follow_up(prior_id=prior_id, new_follow_up=follow_up)
        else:
            store.insert_follow_up(follow_up)
    except sqlite3.IntegrityError as exc:
        if "UNIQUE constraint" in str(exc):
            print(f"You already have an active commitment for {week_iso}. "
                  "Re-run `praxis commit` to retry.", file=sys.stderr)
            return 1
        raise
    print(f'Committed for {week_iso}: "{display}"')
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    """Render the commit prompt, read the user's selection, persist it.

    Resolves the suggestion list from the latest persisted state:
      - The current ISO week's headline_moment.suggested_alternative
        (if a digest has run this week and it has a headline moment).
      - The first canned drill from each of the two weakest dimensions
        (sourced from praxis.scoring.coach.FALLBACK_DRILLS via the
        latest weekly_digests snapshot).
      - 'Keep last week's commitment', when a still-open prior
        commitment exists (latest follow-up with ``outcome='pending'``).
      - 'Write your own', always.

    Mid-week replace (US-023): when an active pending commitment already
    exists for the current ISO week, the handler short-circuits to the
    [r]eplace / [k]eep / [c]ancel preamble before printing the suggestion
    list. ``[k]eep`` and ``[c]ancel`` exit 0 without writing; ``[r]eplace``
    falls through to the suggestion prompt and the eventual persist call
    becomes :meth:`ProfileStore.supersede_and_insert_follow_up`, which
    flips the prior row's ``outcome='superseded'`` + ``superseded_by`` to
    the new row's id in a single transaction.

    When stdin is a TTY (interactive shell), the handler additionally
    reads the user's choice. ``'w'`` opens a validated single-line read
    via :func:`prompt_free_text`; ``'1'``..``'N'`` / ``'k'`` pick a
    pre-built suggestion. On any successful selection the handler writes
    one ``follow_ups`` row via :meth:`ProfileStore.insert_follow_up` with
    ``user_chosen=1``, ``outcome='pending'``, and the verbatim user-facing
    string in ``display_text``.

    Non-TTY invocations (pytest, piped scripts, cron) print the prompt
    and exit 0 without attempting to read. Ctrl-C / Ctrl-D during the
    interactive read also exit 0 cleanly without writing.

    Exit codes:
      0  prompt rendered (and selection handled when interactive).
    """
    import sqlite3

    from praxis.cli.commit import (
        build_commit_suggestions,
        build_user_chosen_follow_up,
        format_commit_prompt,
        format_replace_keep_cancel_preamble,
        load_commit_context,
        prompt_free_text,
        resolve_choice,
        resolve_replace_choice,
    )

    week_iso = current_iso_week()
    store = ProfileStore()

    # Non-interactive (scripts + the menu-bar app): commit a pick or free text
    # and exit, mirroring the interactive replace-or-insert semantics.
    pick = getattr(args, "pick", None)
    free_text = getattr(args, "text", None)
    if pick is not None or free_text is not None:
        return _commit_non_interactive(store, week_iso, pick, free_text)

    # Mid-week replace gate (US-023). Runs BEFORE the suggestion prompt so
    # the user is never surprised by an IntegrityError from a stale active
    # row. Falls through to the normal selection flow on [r]eplace.
    active = store.active_follow_up_for_week(week_iso)
    replace_prior_id: int | None = None
    if active is not None:
        existing_text = active.display_text or active.commitment_text
        print(format_replace_keep_cancel_preamble(existing_text), end="")
        if not sys.stdin.isatty():
            # Non-interactive: print the preamble and exit 0. The user is
            # explicitly informed there is an active commitment, but we
            # don't try to read a choice from a non-TTY stdin.
            return 0
        try:
            raw_replace_choice = input()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        decision = resolve_replace_choice(raw_replace_choice)
        if decision in (None, "keep", "cancel"):
            # Unknown input is treated as "do nothing" -- consistent with
            # the suggestion-prompt's behavior for invalid choices.
            return 0
        # decision == "replace": find the prior row id so the transactional
        # supersede has something to update, then fall through.
        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM follow_ups "
                "WHERE week_iso = ? AND outcome = 'pending' "
                "  AND superseded_by IS NULL "
                "LIMIT 1",
                (week_iso,),
            ).fetchone()
        if row is None:
            # Active row vanished between the two reads (e.g. concurrent
            # CLI run). Fall back to the plain insert path.
            replace_prior_id = None
        else:
            replace_prior_id = int(row[0])

    ctx = load_commit_context(store, week_iso=week_iso)
    suggestions = build_commit_suggestions(ctx)
    print(format_commit_prompt(suggestions), end="")

    if not sys.stdin.isatty():
        return 0

    try:
        raw_choice = input()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0

    chosen = resolve_choice(raw_choice, suggestions)
    if chosen is None:
        return 0

    if chosen.kind == "free_text":
        print("Write your own commitment for this week.")
        try:
            display_text = prompt_free_text()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
    else:
        display_text = chosen.text

    prior = store.latest_follow_up()
    follow_up = build_user_chosen_follow_up(
        week_iso=week_iso,
        suggestion=chosen,
        display_text=display_text,
        prior=prior,
    )
    try:
        if replace_prior_id is not None:
            store.supersede_and_insert_follow_up(
                prior_id=replace_prior_id, new_follow_up=follow_up
            )
        else:
            store.insert_follow_up(follow_up)
    except sqlite3.IntegrityError as exc:
        if "UNIQUE constraint" in str(exc):
            # The partial-unique index fired despite the replace gate above
            # (e.g. a concurrent write between our checks). Surface a friendly
            # hint instead of a traceback.
            print()
            print(
                f"You already have an active commitment for {week_iso}. "
                "Re-run `praxis commit` to retry."
            )
            return 0
        # Any other integrity failure is a real bug, not a benign race -- e.g.
        # a stale narrow outcome CHECK on a DB that predates the 'superseded'
        # migration. Don't mask it behind the retry hint; let it surface.
        raise

    print(f'Your commitment for {week_iso}:')
    print(f'  "{display_text}"')
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

    Throttling (US-018): the first action is a check against
    ``~/.praxis/.last_nudge`` -- if the same (surface, cwd) fired within
    ``[nudge].throttle_minutes`` (default 30) the command exits 0 with empty
    stdout and never opens the DB. A successful surfacing writes a fresh
    timestamp into that file, keyed by ``f"{surface}:{sha1(cwd)}"``.

    Exit codes:
      0  active commitment printed, or no active commitment (silent),
         or throttled (silent).
      4  invariant violated: more than one active row for the current week.
    """
    fmt = getattr(args, "format", "text")
    surface = getattr(args, "surface", "cli")

    # Throttle check runs BEFORE any DB access so a throttled call stays
    # cheap (one stat + one read of the small JSON file) and never opens
    # profile.db. The malformed-JSON recovery happens inside is_throttled.
    cfg = load_config()
    if is_throttled(surface, throttle_minutes=cfg.nudge.throttle_minutes):
        return 0

    week_iso = current_iso_week()
    try:
        active = _resolve_active_commitment(week_iso)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    if active is None:
        # No commitment to surface: don't record a fire, otherwise the
        # next legitimate cue (once the user commits) would be throttled
        # away. Silent-no-op surfaces remain free to retry on every hook.
        return 0
    display_text = active.commitment_text
    if fmt == "text":
        print(f"[Praxis] This week: {display_text}")
    else:
        payload = {
            "hookSpecificOutput": {
                "additionalContext": f"[Praxis] This week's focus: {display_text}",
            },
        }
        print(json.dumps(payload, separators=(",", ":")))
    record_fire(surface)
    return 0


def cmd_rubric(args: argparse.Namespace) -> int:  # noqa: ARG001
    print("\nPRAXIS — SCORING RUBRIC\n")
    for d in RUBRIC:
        print(f"  {d.title}  ({int(d.weight*100)}%)")
        print(f"    {d.description}")
        print(f"    Evidence: {d.evidence}")
        print()
    return 0


_REFLECT_PROMPT_HEADER = 'Did you focus on: "{display_text}"'
_REFLECT_OPTIONS_HINT = "  [y]es / [n]o / [p]artial / [s]kip"
_REFLECT_NOTE_PROMPT = "Optional one-line note (press Enter to skip): "
_REFLECT_NO_COMMITMENT_MSG = (
    "No active commitment this week. Run `praxis commit` to start."
)

# US-025 (--session-end with stdin payload). The 100ms timeout matches
# the AC: AI tools post the Stop-hook JSON payload immediately on
# session end and we must not block them. Notes are user-readable so
# the digest panel can explain why an opt-out row landed.
_HOOK_TIMEOUT_SECONDS = 0.1
_HOOK_PAYLOAD_MISSING_NOTE = "hook payload missing or unparseable"
_HOOK_PAYLOAD_NO_SESSION_NOTE = "hook payload missing session_id"

# US-026 (threshold gating). When the AI tool's Stop hook fires we
# only escalate to an interactive prompt if the session was long enough
# to be worth reflecting on; shorter sessions write a 'skip' row with
# the corresponding note so opt-outs / nuisance sessions are still
# counted in the digest panel.
_TRANSCRIPT_MISSING_NOTE = "transcript missing"
_SESSION_TOO_SHORT_NOTE = "session too short"

# US-027 (detached child). When the parent process is not attached to a
# terminal (the Stop-hook case), the prompt is delegated to a detached
# child that opens /dev/tty (POSIX) or CONIN$/CONOUT$ (Windows) on its
# own. If the child cannot reach a controlling terminal, it writes a
# 'parent terminal closed' skip row so the session is still observable.
_PARENT_TERMINAL_CLOSED_NOTE = "parent terminal closed"
_SPAWN_FAILED_NOTE = "failed to spawn reflect child"

_SELF_REPORT_BY_CHOICE: dict[str, SelfReport] = {
    "y": "yes",
    "yes": "yes",
    "n": "no",
    "no": "no",
    "p": "partial",
    "partial": "partial",
    "s": "skip",
    "skip": "skip",
}


def _read_self_report_choice(stream: Any) -> SelfReport | None:
    """Read one line from ``stream`` and map to a self_report value.

    Returns None on EOF / empty input so the caller can decide whether
    to re-prompt or fall back to 'skip'. Recognized choices are case-
    insensitive and accept either the single letter or the full word.
    """
    raw = stream.readline()
    if not raw:
        return None
    choice = raw.strip().lower()
    if not choice:
        return None
    return _SELF_REPORT_BY_CHOICE.get(choice)


def _read_optional_note(stream: Any) -> str | None:
    """Read one line of optional note text; empty line -> None.

    Only the first line is kept (we strip a trailing newline). Callers
    pass None for skip; this helper is invoked only on yes/no/partial.
    """
    raw = stream.readline()
    if not raw:
        return None
    stripped = raw.rstrip("\r\n").strip()
    return stripped or None


def _read_hook_payload(
    stream: Any,
    timeout_seconds: float = _HOOK_TIMEOUT_SECONDS,
) -> str | None:
    """Read a Stop-hook JSON payload from ``stream`` within ``timeout_seconds``.

    Returns the raw text (caller parses JSON) or None on timeout / EOF /
    error. The 100ms default timeout matches the US-025 AC: AI tools
    post the hook payload immediately on session end and we must not
    block them. On POSIX the timeout is enforced via ``select.select``;
    on Windows or on streams without a real file descriptor (test
    StringIOs), the read is unconditional and returns whatever is
    available -- in practice the hook closes stdin right after writing
    so EOF arrives quickly anyway.
    """
    fileno: int | None = None
    try:
        fileno = stream.fileno()
    except (AttributeError, OSError, ValueError):
        fileno = None

    if fileno is not None and sys.platform != "win32":
        try:
            ready, _, _ = select.select([fileno], [], [], timeout_seconds)
        except (OSError, ValueError):
            return None
        if not ready:
            return None

    try:
        data = stream.read()
    except (OSError, ValueError):
        return None
    if not data:
        return None
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return data


def _parse_hook_payload(raw: str | None) -> dict[str, Any] | None:
    """Parse a hook payload string. None on missing / malformed.

    Duck-typed: we accept any top-level JSON object regardless of which
    keys are present. The caller decides whether the required fields
    for Claude Code (session_id, transcript_path, cwd, hook_event_name)
    or Codex (session_id, cwd, hook_event_name) are satisfied.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _extract_session_id(payload: dict[str, Any]) -> str | None:
    """Pluck ``session_id`` from a hook payload if it's a non-empty string.

    Both Claude Code and Codex shapes name this field identically, so
    the duck-typed check on ``session_id`` covers both providers
    without per-shape branching.
    """
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid
    return None


def _extract_transcript_path(payload: dict[str, Any]) -> Path | None:
    """Return the transcript_path from a hook payload as a Path.

    Claude Code's Stop hook posts ``transcript_path`` pointing at a
    JSONL file on disk; Codex's Stop hook omits this field entirely.
    Returns None when the field is absent, blank, or not a string so
    callers can distinguish "no transcript was sent" (Codex shape) from
    "transcript was sent but unreadable" (Claude shape, file missing).
    """
    raw = payload.get("transcript_path")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    return Path(text)


def _read_transcript_stats(transcript_path: Path) -> tuple[int, float] | None:
    """Return ``(user_turns, elapsed_seconds)`` for a Claude Code transcript.

    The transcript is the JSONL file Claude Code posts as
    ``transcript_path`` in its Stop hook. Each line is one event; we
    only care about two facts:

      * how many ``type == "user"`` entries had non-empty content (the
        "user turns" the AC counts), and
      * the elapsed time between the earliest and latest ``timestamp``
        on the file (any entry contributes its timestamp, not just user
        turns -- the user can sit idle while the assistant works).

    Returns ``None`` if the file can't be opened (the caller treats
    this the same as 'transcript missing'). Malformed JSON lines are
    skipped silently so a partially-written transcript doesn't blow up
    the read; elapsed time falls back to 0.0 when there are no
    timestamps. The parser is intentionally tolerant: this code path
    runs under the Stop hook and we must never crash the AI tool.
    """
    user_turns = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue

                if entry.get("type") == "user":
                    message = entry.get("message") or {}
                    content = message.get("content") if isinstance(message, dict) else None
                    if _has_user_text(content):
                        user_turns += 1

                ts_raw = entry.get("timestamp")
                if isinstance(ts_raw, str):
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        ts = None
                    if ts is not None:
                        if first_ts is None or ts < first_ts:
                            first_ts = ts
                        if last_ts is None or ts > last_ts:
                            last_ts = ts
    except OSError:
        return None

    if first_ts is not None and last_ts is not None:
        elapsed = (last_ts - first_ts).total_seconds()
    else:
        elapsed = 0.0
    return (user_turns, elapsed)


def _has_user_text(content: object) -> bool:
    """Best-effort check that a Claude Code ``message.content`` has text.

    Claude Code's content is either a plain string or a list of typed
    blocks ({type: "text", text: ...} et al.). We count the turn as a
    real user turn only if at least one text block has non-whitespace
    characters -- empty 'system' frames (tool_use_result with no text)
    shouldn't bump the user-turn count.
    """
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return True
    return False


def cmd_reflect(args: argparse.Namespace) -> int:
    """Reflect on this week's active commitment.

    Three modes:
      * Interactive (default, US-024): prompt [y]es / [n]o / [p]artial /
        [s]kip on stdin, then accept an optional one-line note. Inserts
        one row into ``session_reflections`` so opt-outs are still
        counted.
      * --session-end (US-025/US-026): read a JSON Stop-hook payload
        from stdin (Claude Code or Codex shape) within a 100ms timeout
        and either persist a skip row (degenerate payload / threshold
        gate fail) or spawn a detached child for the interactive prompt
        (US-027). Never blocks the AI tool; every code path exits 0.
      * --child (US-027): re-entry point used by the detached child
        spawn. Opens /dev/tty (POSIX) or CONIN$/CONOUT$ (Windows),
        reuses ``_run_interactive_reflect`` against those streams, and
        falls back to a 'parent terminal closed' skip row if no
        controlling terminal is available.

    Exit codes (interactive mode):
      0 -- a reflection row was inserted, or no active commitment
           exists for the current week (silent no-op with a hint).
      1 -- input parsing gave up (>3 invalid choices) or the
           commitment invariant was violated.

    The --session-end and --child branches NEVER return exit code 2:
    Claude Code interprets exit 2 as a 'block' signal that aborts the
    AI tool's session; reflect must stay out of that codespace.
    """
    if getattr(args, "child", False):
        return _cmd_reflect_child(args)
    if getattr(args, "session_end", False):
        return _cmd_reflect_session_end(sys.stdin)

    store = ProfileStore()
    week_iso = current_iso_week()
    try:
        active = store.load_active_commitment(week_iso)
    except MultipleActiveCommitmentsError as exc:
        print(f"praxis reflect: {exc}", file=sys.stderr)
        return 1

    if active is None:
        print(_REFLECT_NO_COMMITMENT_MSG)
        return 0

    # Non-interactive (scripts + the menu-bar app): record straight away.
    set_value = getattr(args, "set_value", None)
    if set_value is not None:
        note = (getattr(args, "note", None) or "").strip() or None
        store.insert_session_reflection(
            session_stable_id=f"manual:{week_iso}",
            follow_up_id=active.follow_up_id,
            self_report=set_value,
            note=note,
        )
        print(f"Reflected: {set_value}.")
        return 0

    return _run_interactive_reflect(store, active, sys.stdin, sys.stdout)


def _cmd_reflect_session_end(stdin: Any) -> int:
    """Handle ``praxis reflect --session-end``: parse a Stop-hook JSON
    payload from stdin and persist a reflection row.

    Never blocks the AI tool: every code path exits 0. When the payload
    is missing, malformed, or has no session_id, a skip row is still
    written with a descriptive note so opt-outs / hook failures are
    counted in the digest panel. When the payload includes a
    ``transcript_path`` (Claude Code shape), US-026 threshold gating
    checks the transcript before reaching the happy path:

      * file missing on disk -> skip + 'transcript missing'.
      * user_turns < turns_min OR elapsed_seconds < elapsed_seconds_min
        -> skip + 'session too short'.

    Codex-shape payloads (no transcript_path) bypass the threshold gate.

    Happy path (US-027): spawn a detached child via ``subprocess.Popen``
    with ``start_new_session=True`` (POSIX) /
    ``CREATE_NEW_PROCESS_GROUP`` (Windows). The parent returns 0
    immediately so the AI tool's hook completes within the timeout. The
    child opens /dev/tty (or CONIN$/CONOUT$) and runs the interactive
    prompt; if the spawn fails (no praxis binary, OS rejection), the
    parent writes a fallback skip row so the session is still
    observable.

    Exit code is always 0 -- the AI tool must not see a block signal.
    """
    store = ProfileStore()
    week_iso = current_iso_week()
    try:
        active = store.load_active_commitment(week_iso)
    except MultipleActiveCommitmentsError:
        # Can't pick a follow_up_id without violating the invariant.
        # Silent exit 0 -- printing to stderr here would pollute the
        # AI tool's session log.
        return 0
    if active is None:
        # Nothing to reflect on this week. Silent exit 0.
        return 0

    raw = _read_hook_payload(stdin)
    payload = _parse_hook_payload(raw)
    fallback_stable_id = f"session-end:{week_iso}"
    if payload is None:
        store.insert_session_reflection(
            session_stable_id=fallback_stable_id,
            follow_up_id=active.follow_up_id,
            self_report="skip",
            note=_HOOK_PAYLOAD_MISSING_NOTE,
        )
        return 0

    session_id = _extract_session_id(payload)
    if session_id is None:
        store.insert_session_reflection(
            session_stable_id=fallback_stable_id,
            follow_up_id=active.follow_up_id,
            self_report="skip",
            note=_HOOK_PAYLOAD_NO_SESSION_NOTE,
        )
        return 0

    transcript_path = _extract_transcript_path(payload)
    if transcript_path is not None:
        reflect_cfg = _load_reflect_config()
        gate_note = _check_transcript_threshold(transcript_path, reflect_cfg)
        if gate_note is not None:
            store.insert_session_reflection(
                session_stable_id=session_id,
                follow_up_id=active.follow_up_id,
                self_report="skip",
                note=gate_note,
            )
            return 0

    # Happy path: spawn the detached child and return 0 immediately.
    # The child opens its own TTY and writes the row. We don't .wait()
    # the child so the parent unblocks within the OS spawn time.
    cwd_value = payload.get("cwd")
    cwd_str: str | None = cwd_value if isinstance(cwd_value, str) and cwd_value else None
    spawned = _spawn_reflect_child(
        follow_up_id=active.follow_up_id,
        session_id=session_id,
        transcript_path=transcript_path,
        cwd=cwd_str,
    )
    if not spawned:
        # Spawn failed (no praxis binary on PATH, OS rejected the
        # process, etc.). Fall back to a skip row so the session is
        # still observable instead of silently lost.
        store.insert_session_reflection(
            session_stable_id=session_id,
            follow_up_id=active.follow_up_id,
            self_report="skip",
            note=_SPAWN_FAILED_NOTE,
        )
    return 0


def _spawn_reflect_child(
    *,
    follow_up_id: int,
    session_id: str,
    transcript_path: Path | None,
    cwd: str | None,
) -> bool:
    """Spawn the detached child for the interactive reflect prompt.

    The child runs ``praxis reflect --child --follow-up-id <id>
    --session-id <sid>`` (plus optional --transcript-path / --cwd) in
    its own process group / session, with stdin/stdout/stderr pointed
    at /dev/null. The child re-opens /dev/tty (POSIX) or
    CONIN$/CONOUT$ (Windows) to talk to the user.

    Returns True on successful spawn; False on OSError so the caller
    can write a fallback skip row. Tests monkeypatch this function to
    capture spawn invocations without creating real subprocesses.

    The Popen call returns immediately -- we deliberately do NOT call
    .wait(), so the parent returns within the OS spawn time (well
    under the 5s hook_timeout_seconds budget on any modern system).
    """
    argv = _resolve_reflect_child_command() + [
        "reflect",
        "--child",
        "--follow-up-id",
        str(follow_up_id),
        "--session-id",
        session_id,
    ]
    if transcript_path is not None:
        argv.extend(["--transcript-path", str(transcript_path)])
    if cwd:
        argv.extend(["--cwd", cwd])

    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP detaches from the parent's console
        # so the child survives parent exit and the AI tool isn't
        # blocked waiting for descendant processes.
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        popen_kwargs["creationflags"] = creationflags
    else:
        # start_new_session calls setsid() so the child becomes its own
        # session leader and can open /dev/tty as the controlling
        # terminal (Claude Code's Stop hook closes the parent's stdin
        # but the user's terminal is still reachable).
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(argv, **popen_kwargs)
    except (OSError, ValueError):
        return False
    return True


def _resolve_reflect_child_command() -> list[str]:
    """Return the argv prefix that re-launches ``praxis`` for the child.

    Mirrors ``install_weekly._resolve_praxis_command``: prefer the
    installed console script, fall back to ``[sys.executable, '-m',
    'praxis.cli']`` so editable / venv installs still work.
    """
    found = shutil.which("praxis")
    if found:
        return [found]
    return [sys.executable, "-m", "praxis.cli"]


def _cmd_reflect_child(args: argparse.Namespace) -> int:
    """Run the interactive prompt as the detached child (US-027).

    The parent process spawned us with explicit ``--follow-up-id`` and
    ``--session-id`` so we don't have to re-resolve the active
    commitment. We open /dev/tty (POSIX) or CONIN$/CONOUT$ (Windows) for
    stdin/stdout; when no controlling terminal is available (parent's
    terminal closed before we got there), we still write a 'parent
    terminal closed' skip row so the session is observable.

    Exit codes (always 0 in practice):
      0 -- a reflection row was inserted (interactive write OR the
           fallback 'parent terminal closed' skip).

    We never return exit code 2 -- the AI tool already moved on, but we
    keep reflect's exit codes inside {0, 1} to honor the same contract
    the parent does.
    """
    follow_up_id = int(getattr(args, "follow_up_id", 0) or 0)
    session_id = str(getattr(args, "session_id", "") or "")
    transcript_path = getattr(args, "transcript_path", None)
    cwd = getattr(args, "cwd", None)
    # transcript_path and cwd are accepted for forward-compat with
    # richer prompts (we may show the cwd in the question); the
    # underscored locals quiet the unused-variable warning today.
    _ = transcript_path
    _ = cwd

    store = ProfileStore()
    if follow_up_id <= 0 or not session_id:
        # Defensive: a malformed invocation shouldn't crash the child.
        return 0

    active = store.load_commitment_by_id(follow_up_id)
    if active is None:
        # The commitment was removed between parent spawn and child
        # start; nothing to prompt about.
        return 0

    tty_stdin, tty_stdout = _open_controlling_terminal()
    if tty_stdin is None or tty_stdout is None:
        # No controlling terminal reachable. Write a skip row so the
        # session is still observable in the digest panel.
        store.insert_session_reflection(
            session_stable_id=session_id,
            follow_up_id=follow_up_id,
            self_report="skip",
            note=_PARENT_TERMINAL_CLOSED_NOTE,
        )
        return 0

    try:
        # _run_interactive_reflect writes 'manual:<week>' as the stable
        # id today; override it to the real session_id so the row
        # joins back to the AI tool's session.
        return _run_interactive_reflect_with_session(
            store,
            active,
            tty_stdin,
            tty_stdout,
            session_stable_id=session_id,
        )
    finally:
        for stream in (tty_stdin, tty_stdout):
            try:
                stream.close()
            except OSError:
                pass


def _open_controlling_terminal() -> tuple[Any, Any]:
    """Open the controlling terminal for read+write.

    Returns ``(stdin_stream, stdout_stream)`` on success, ``(None,
    None)`` if no TTY is reachable. POSIX uses /dev/tty; Windows uses
    CONIN$ / CONOUT$ (the special device names the console subsystem
    exposes for the current console).

    Best-effort: any OSError (no controlling terminal, permissions,
    closed-stdin under daemon-style spawn) returns the (None, None)
    sentinel so the caller falls back to the 'parent terminal closed'
    skip row.
    """
    if sys.platform == "win32":
        try:
            stdin_stream = open("CONIN$", "r", encoding="utf-8")
        except OSError:
            return (None, None)
        try:
            stdout_stream = open("CONOUT$", "w", encoding="utf-8")
        except OSError:
            try:
                stdin_stream.close()
            except OSError:
                pass
            return (None, None)
        return (stdin_stream, stdout_stream)

    try:
        stdin_stream = open("/dev/tty", "r", encoding="utf-8")
    except OSError:
        return (None, None)
    try:
        stdout_stream = open("/dev/tty", "w", encoding="utf-8")
    except OSError:
        try:
            stdin_stream.close()
        except OSError:
            pass
        return (None, None)
    return (stdin_stream, stdout_stream)


def _run_interactive_reflect_with_session(
    store: ProfileStore,
    active: ActiveCommitment,
    stdin: Any,
    stdout: Any,
    *,
    session_stable_id: str,
) -> int:
    """Same as ``_run_interactive_reflect`` but stamps a custom session id.

    Used by the US-027 child so the row joins back to the AI tool's
    real session id rather than the ``manual:<week>`` placeholder the
    bare-interactive path uses.
    """
    print(
        _REFLECT_PROMPT_HEADER.format(display_text=active.display_text),
        file=stdout,
    )
    print(_REFLECT_OPTIONS_HINT, file=stdout)
    stdout.flush()

    choice: SelfReport | None = None
    for _ in range(3):
        choice = _read_self_report_choice(stdin)
        if choice is not None:
            break
        print(
            "Please answer with y, n, p, or s.",
            file=stdout,
        )
        stdout.flush()
    if choice is None:
        # Child cannot reach a valid answer. Write a skip row so the
        # session is still observable -- the parent already returned
        # so we never affect the AI tool's exit code.
        store.insert_session_reflection(
            session_stable_id=session_stable_id,
            follow_up_id=active.follow_up_id,
            self_report="skip",
            note=_PARENT_TERMINAL_CLOSED_NOTE,
        )
        return 0

    note: str | None = None
    if choice != "skip":
        print(_REFLECT_NOTE_PROMPT, end="", file=stdout)
        stdout.flush()
        note = _read_optional_note(stdin)

    store.insert_session_reflection(
        session_stable_id=session_stable_id,
        follow_up_id=active.follow_up_id,
        self_report=choice,
        note=note,
    )
    print(f"Recorded reflection: {choice}", file=stdout)
    return 0


def _load_reflect_config() -> ReflectConfig:
    """Load the [reflect] section, falling back to defaults on errors.

    A malformed ``~/.praxis/config.toml`` (invalid TOML, negative
    threshold, etc.) MUST NOT crash the Stop hook -- the AI tool sees a
    non-zero exit as a block signal. We swallow any load error and use
    the documented defaults so the gate still applies sensibly.
    """
    try:
        return load_config().reflect
    except (OSError, ValueError, TypeError):
        return ReflectConfig()


def _check_transcript_threshold(
    transcript_path: Path,
    cfg: ReflectConfig,
) -> str | None:
    """Return the skip-note for a sub-threshold transcript, else None.

    The two branches encode the AC for US-026:
      * file missing on disk -> 'transcript missing'
      * stats below either threshold -> 'session too short'
      * meets both thresholds -> None (caller falls through to the
        happy path)
    """
    if not transcript_path.is_file():
        return _TRANSCRIPT_MISSING_NOTE
    stats = _read_transcript_stats(transcript_path)
    if stats is None:
        # Unreadable transcript: treat as 'missing' so the user-facing
        # note matches the AC's wording.
        return _TRANSCRIPT_MISSING_NOTE
    user_turns, elapsed_seconds = stats
    if user_turns < cfg.turns_min or elapsed_seconds < cfg.elapsed_seconds_min:
        return _SESSION_TOO_SHORT_NOTE
    return None


def _run_interactive_reflect(
    store: ProfileStore,
    active: ActiveCommitment,
    stdin: Any,
    stdout: Any,
) -> int:
    """Drive the interactive prompt against the given streams.

    Split out from ``cmd_reflect`` so tests can pass in StringIOs
    without monkeypatching sys.stdin/stdout and so the --session-end
    detached-child code path (US-027) can reuse it against /dev/tty.
    """
    print(
        _REFLECT_PROMPT_HEADER.format(display_text=active.display_text),
        file=stdout,
    )
    print(_REFLECT_OPTIONS_HINT, file=stdout)
    stdout.flush()

    # Allow a few retries on invalid choices to forgive typos, but
    # don't loop forever -- a piped/EOF stream must terminate.
    choice: SelfReport | None = None
    for _ in range(3):
        choice = _read_self_report_choice(stdin)
        if choice is not None:
            break
        print(
            "Please answer with y, n, p, or s.",
            file=stdout,
        )
        stdout.flush()
    if choice is None:
        print(
            "praxis reflect: no valid choice received; aborting "
            "without writing a reflection.",
            file=sys.stderr,
        )
        return 1

    note: str | None = None
    if choice != "skip":
        print(_REFLECT_NOTE_PROMPT, end="", file=stdout)
        stdout.flush()
        note = _read_optional_note(stdin)

    # No associated AI session in the interactive path (US-024). Mark
    # the row as 'manual:<iso-week>' so reports can distinguish opt-in
    # reflections from session-end reflections (US-025). The id is
    # human-readable but not unique on its own; session_reflections.id
    # (the autoincrement PK) is the real key.
    session_stable_id = f"manual:{active.follow_up.week_iso}"
    store.insert_session_reflection(
        session_stable_id=session_stable_id,
        follow_up_id=active.follow_up_id,
        self_report=choice,
        note=note,
    )
    print(f"Recorded reflection: {choice}", file=stdout)
    return 0


def cmd_install_weekly(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Generate and load the macOS LaunchAgent for ``praxis review --notify``.

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


def cmd_install_coach(args: argparse.Namespace) -> int:
    """Detect supported AI coding tools and install the coaching hooks.

    Per AC US-028: prompts ``Found <Tool>. Install the Praxis coaching
    hook? [Y/n]:`` for each detected tool. ``--yes`` skips prompting;
    ``--tool NAME`` restricts to a single tool (still prompts unless
    paired with ``--yes``); ``--all`` proceeds regardless of detection
    (and is mutually exclusive with ``--tool``).

    Exit codes:
      0  -- happy path, including the "no tools detected" branch.
      1  -- invalid ``--tool`` argument.
    """
    from praxis.cli.install_coach import run_install_coach

    return run_install_coach(
        assume_yes=getattr(args, "yes", False),
        tool=getattr(args, "tool", None),
        all_tools=getattr(args, "all", False),
    )


def cmd_uninstall_coach(args: argparse.Namespace) -> int:
    """Symmetric teardown of the coaching hooks installed by ``install-coach``.

    Per AC US-032: removes only blocks/entries carrying
    ``_praxisManaged: true`` (Claude Code, Codex) or bounded by the
    ``praxisManaged`` markdown markers (Copilot); user-authored content
    at the same event names is preserved. ``--yes`` skips prompts;
    ``--tool NAME`` restricts to one tool; ``--all`` iterates every
    known tool regardless of detection. On a system where Praxis was
    never installed (no managed content anywhere) the command prints
    ``Nothing to uninstall.`` and exits 0 without any file writes.

    Exit codes:
      0  -- happy path (including the "nothing to uninstall" branch).
      1  -- invalid ``--tool`` argument.
    """
    from praxis.cli.install_coach import run_uninstall_coach

    return run_uninstall_coach(
        assume_yes=getattr(args, "yes", False),
        tool=getattr(args, "tool", None),
        all_tools=getattr(args, "all", False),
    )


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


_LOOP_HELP_EPILOG = """\
Loop:
  commit                  Pick this week's focus (Commit step).
  nudge                   Print the active commitment for in-session cueing (Cue step).
  reflect                 Post-session check-in (Reflect step).
  review                  Render this week's digest (Review step; was 'week' in v0.2).
  install-coach           Wire the loop into Claude Code / Codex / Copilot.

More:
  scan                    Scan + score newly-discovered sessions (no digest render).
  re-score                Re-run the frontier judge for one session.
  baseline                Print the current 90-day baseline (read-only).
  history                 List past weekly digests (read-only).
  show                    Render a past week's digest from persisted data.
  report                  Open or print the legacy v0.1 HTML report.
  open                    Open this week's digest in the browser.
  last                    Print the latest digest's path / ISO week / trajectory.
  status                  Show what's been scored, when, and where.
  rubric                  Print the scoring rubric and weights.
  follow-up               Show the most recent weekly commitment and its outcome.
  models                  List or describe the built-in model cards.
  config                  View / --get / --set ~/.praxis/config.toml.
  install-weekly          Install the macOS LaunchAgent for `praxis review --notify`.
  uninstall-weekly        Remove the weekly LaunchAgent.
  shell-nudge             Print the shell snippet for `eval` in ~/.zshrc / ~/.bashrc.
  install-shell-nudge     Append the shell-nudge eval line to RC files.
  uninstall-shell-nudge   Remove the shell-nudge eval line.

Run 'praxis <command> --help' for command-specific options.
"""


class _GroupedSubparsersFormatter(argparse.RawDescriptionHelpFormatter):
    """Help formatter that hides argparse's auto-generated subparser list.

    Each loop / more subparser is registered with ``help=argparse.SUPPRESS``,
    but that only blanks the per-row help column; the row itself (and the
    ``{commit,nudge,...}`` metavar line) still renders. Skipping the
    ``_SubParsersAction`` here removes the auto-list entirely so the
    curated ``Loop:`` / ``More:`` epilog is the only canonical listing
    the user sees in ``praxis --help`` (AC US-044 #1).
    """

    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):
            return ""
        return super()._format_action(action)


def cmd_coming_soon(args: argparse.Namespace) -> int:
    """Stub handler for loop verbs that have not been wired up yet.

    `praxis commit`, `praxis nudge`, `praxis reflect`, and
    `praxis install-coach` ship as registered subparsers (so they show
    up in `praxis --help` under the "Loop" heading) but their handlers
    have not landed yet. Invocations print a friendly note pointing the
    user at the live `praxis review` verb and exit 0 so scripted users
    do not see a crash.

    See PLAN.md for the schedule that lands each verb's real handler.
    """
    verb = getattr(args, "cmd", "<unknown>")
    print(
        f"`praxis {verb}` is part of the Commit -> Cue -> Reflect -> Review "
        f"loop and is not wired up yet."
    )
    print(
        "Run `praxis review` to render this week's digest; see README.md "
        "(`## The loop`) for the full plan."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="praxis",
        description="Your AI usage coach. Commit -> Cue -> Reflect -> Review.",
        formatter_class=_GroupedSubparsersFormatter,
        epilog=_LOOP_HELP_EPILOG,
    )
    p.add_argument("--version", action="version", version=f"praxis {__version__}")
    p.add_argument(
        "--debug",
        action="store_true",
        help="On an unexpected error, print the full traceback (same as PRAXIS_DEBUG=1).",
    )
    # ``help=argparse.SUPPRESS`` on every subparser hides the auto-generated
    # "{commit,nudge,...}" list so the curated epilog above is the canonical
    # listing the user sees. The ordering of ``add_parser`` calls below is
    # purely cosmetic in that mode, but we keep loop verbs first so any
    # downstream tooling that introspects ``sub.choices`` sees them in
    # narrative order too.
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    # --- Loop verb: review ---
    # The commit / nudge / reflect / install-coach parsers are registered
    # later in this function (added by their respective branches: US-019,
    # US-020..023, US-024..027, US-028..032). US-044's "loop verbs first"
    # surface organisation is reflected in the help text + the README
    # masthead; we keep `review`'s help visible (rather than SUPPRESS) so
    # `praxis --help` advertises the primary read verb.

    review = sub.add_parser(
        "review",
        help="Render this week's digest (the Review step of the loop).",
        description=(
            "Render this week's digest -- what changed against last week's "
            "commitment (the Review step of the Commit -> Cue -> Reflect -> "
            "Review loop). Was named `week` in v0.2; renamed in US-033/US-044 "
            "to match the README masthead. The terminal output always renders; "
            "HTML and notifications are opt-in via flags."
        ),
    )
    review.add_argument(
        "--week",
        type=str,
        default=None,
        metavar="ISO",
        help=(
            "Render a past ISO week (e.g. 2026-W21). Scanning and scoring "
            "are skipped; the snapshot is rebuilt from the persisted DB rows."
        ),
    )
    review.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compute everything but do not write to the DB or any files. "
            "Useful for previewing the digest against current data."
        ),
    )
    review.add_argument(
        "--frontier-only",
        action="store_true",
        help=(
            "Force every session through the frontier judge (spec 9.6). "
            "The two-pass judge wiring lands in a separate story; the "
            "flag is wired here so the CLI seam stays stable."
        ),
    )
    review.add_argument(
        "--explain-judging",
        action="store_true",
        help=(
            "Print the pass-1 confidence distribution after the digest "
            "(spec 9.6). Will note when no distribution was recorded."
        ),
    )
    review.add_argument(
        "--notify",
        action="store_true",
        help=(
            "Post a macOS notification when the digest is ready. Silent "
            "no-op on non-Darwin platforms (spec 13.2). Implies "
            "--write-html."
        ),
    )
    review.add_argument(
        "--write-html",
        action="store_true",
        help=(
            "Write the HTML digest to ~/.praxis/weeks/<iso>.html "
            "(spec 13.1). The terminal render is always printed."
        ),
    )
    review.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Skip the [k]eep / [n]ew / [d]igest prompt that follows the "
            "terminal masthead (spec section 2). Always implied by the "
            "scheduled --notify path; pass this flag for scripted runs "
            "that should never block on stdin."
        ),
    )
    review.add_argument(
        "--max-new",
        type=int,
        default=50,
        metavar="N",
        help=(
            "Cap on newly-discovered sessions to deep-score this run "
            "(default: 50). Already-scored stable_ids are skipped before "
            "the cap is applied. Use 0 (or a negative value) to disable "
            "the cap; defaults to the same number as `praxis scan` so a "
            "stray `praxis review` cannot silently kick off N x LLM "
            "calls on a busy week (issue #5)."
        ),
    )
    review.set_defaults(func=cmd_review)

    # --- More (operational + read-only) ---

    scan = sub.add_parser(
        "scan",
        help=argparse.SUPPRESS,
        description=(
            "Discover new sessions and run the judge against them, "
            "persisting results into ~/.praxis/profile.db. Prints a "
            "one-line summary; the digest itself lives behind "
            "'praxis review' / 'praxis show <iso>'."
        ),
    )
    scan.add_argument("--since-days", type=int, default=30,
                      help="Only consider session files modified in the last N days.")
    scan.add_argument("--max-new", type=int, default=50,
                      help="Cap on newly-discovered sessions to deep-score per run.")
    scan.set_defaults(func=cmd_scan)

    rescore = sub.add_parser(
        "re-score",
        help=argparse.SUPPRESS,
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
        help=argparse.SUPPRESS,
        description=(
            "Read-only summary of the 90-day rolling baseline that the "
            "weekly digest panel uses. Renders '--' when the user has "
            "less than 14 days of data (spec section 8.4)."
        ),
    )
    base.set_defaults(func=cmd_baseline)

    hist = sub.add_parser(
        "history",
        help=argparse.SUPPRESS,
        description=(
            "Enumerate the ISO weeks present in session_scores with "
            "their session count and mean overall score. Drill into one "
            "with 'praxis show <week_iso>'."
        ),
    )
    hist.set_defaults(func=cmd_history)

    show = sub.add_parser(
        "show",
        help=argparse.SUPPRESS,
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

    rep = sub.add_parser("report", help=argparse.SUPPRESS)
    rep.add_argument("--print", action="store_true",
                     help="Print HTML to stdout instead of opening browser.")
    rep.set_defaults(func=cmd_report)

    opn = sub.add_parser(
        "open",
        help=argparse.SUPPRESS,
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
        help=argparse.SUPPRESS,
        description=(
            "Read-only metadata about ~/.praxis/latest.html. Does not "
            "open a browser window. Use --path-only for scripting "
            "(e.g. `open \"$(praxis last --path-only)\"`)."
        ),
    )
    lst.add_argument("--path-only", action="store_true",
                     help="Print only the absolute path (one line, no labels).")
    lst.add_argument("--json", action="store_true",
                     help="Emit machine-readable JSON instead of text.")
    lst.set_defaults(func=cmd_last)

    sts = sub.add_parser("status", help=argparse.SUPPRESS)
    sts.add_argument("--json", action="store_true",
                     help="Emit machine-readable JSON instead of text.")
    sts.set_defaults(func=cmd_status)

    doc = sub.add_parser(
        "doctor",
        help="Check API keys, database health, hooks, and schedule.",
        description=(
            "Run a health check across API keys, the profile database "
            "(presence + integrity + scored count), this week's focus, the "
            "coaching hooks, and the weekly schedule. Prints what's healthy "
            "and what to fix. Exits non-zero only on a critical problem."
        ),
    )
    doc.set_defaults(func=cmd_doctor)

    rub = sub.add_parser("rubric", help=argparse.SUPPRESS)
    rub.set_defaults(func=cmd_rubric)

    fup = sub.add_parser("follow-up", help=argparse.SUPPRESS)
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
    nudge.add_argument(
        "--surface",
        type=str,
        default="cli",
        help=(
            "Surface identifier used for throttling. The throttle file "
            "(~/.praxis/.last_nudge) is keyed by (surface, sha1(cwd)); "
            "callers using the default share a single throttle entry so "
            "a shell startup right after a SessionStart hook stays silent."
        ),
    )
    nudge.set_defaults(func=cmd_nudge)

    cmt = sub.add_parser(
        "commit",
        help="Pick a coaching commitment for this ISO week.",
        description=(
            "Print the numbered commitment-suggestion prompt for the "
            "current ISO week. Sources: this week's headline moment, "
            "drills for the two weakest dimensions, an optional "
            "'Keep last week' option when a still-open commitment "
            "exists, and the 'Write your own' fallback."
        ),
    )
    cmt.add_argument(
        "--pick",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Commit suggestion N (1-based, from the printed list) non-"
            "interactively and exit. For scripts and the menu-bar app."
        ),
    )
    cmt.add_argument(
        "--text",
        type=str,
        default=None,
        help="Commit your own free-text focus non-interactively (<=280 chars) and exit.",
    )
    cmt.set_defaults(func=cmd_commit)

    rfl = sub.add_parser(
        "reflect",
        help=(
            "Reflect on this week's active commitment "
            "(interactive 2-question prompt)."
        ),
        description=(
            "Look up the active follow_ups row for the current ISO "
            "week, prompt the user with the commitment's display_text, "
            "and record one row in session_reflections (yes / no / "
            "partial / skip plus an optional one-line note). When no "
            "active commitment exists for this week, exits 0 with a "
            "hint to run `praxis commit` first."
        ),
    )
    rfl.add_argument(
        "--session-end",
        action="store_true",
        dest="session_end",
        help=(
            "Read a Stop-hook JSON payload from stdin (Claude Code or "
            "Codex shape) instead of running the interactive prompt. "
            "Used by editor hooks; never blocks the AI tool. Writes a "
            "skip row with a descriptive note when the payload is "
            "missing / malformed / has no session_id, and always exits 0."
        ),
    )
    # The --child path is an internal re-entry point used by the
    # detached child the parent spawns in --session-end mode (US-027).
    # The four flags below carry the state the parent resolved so the
    # child doesn't have to re-derive it from scratch.
    rfl.add_argument(
        "--child",
        action="store_true",
        dest="child",
        help=argparse.SUPPRESS,
    )
    rfl.add_argument(
        "--follow-up-id",
        type=int,
        default=0,
        dest="follow_up_id",
        help=argparse.SUPPRESS,
    )
    rfl.add_argument(
        "--session-id",
        type=str,
        default="",
        dest="session_id",
        help=argparse.SUPPRESS,
    )
    rfl.add_argument(
        "--transcript-path",
        type=str,
        default=None,
        dest="transcript_path",
        help=argparse.SUPPRESS,
    )
    rfl.add_argument(
        "--cwd",
        type=str,
        default=None,
        dest="cwd",
        help=argparse.SUPPRESS,
    )
    rfl.add_argument(
        "--set",
        choices=("yes", "no", "partial", "skip"),
        dest="set_value",
        default=None,
        help=(
            "Record the reflection non-interactively (for scripts and the menu-"
            "bar app) and exit 0, instead of prompting."
        ),
    )
    rfl.add_argument(
        "--note",
        type=str,
        default=None,
        help="Optional one-line note to store with --set.",
    )
    rfl.set_defaults(func=cmd_reflect)

    mod = sub.add_parser("models", help=argparse.SUPPRESS)
    mod.add_argument("--show", type=str, default=None,
                     help="Show full details for one model card (by id or alias).")
    mod.set_defaults(func=cmd_models)

    iw = sub.add_parser(
        "install-weekly",
        help=argparse.SUPPRESS,
        description=(
            "Generate ~/Library/LaunchAgents/co.praxis.weekly.plist from "
            "the schedule in ~/.praxis/config.toml and load it via "
            "launchctl, so `praxis review --notify` runs on schedule. "
            "Idempotent. On non-macOS this command prints the "
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
        help=argparse.SUPPRESS,
        description=(
            "Run 'launchctl unload' against "
            "~/Library/LaunchAgents/co.praxis.weekly.plist and delete the "
            "file. Exits 0 even when no job is currently installed."
        ),
    )
    uw.set_defaults(func=cmd_uninstall_weekly)

    sn = sub.add_parser(
        "shell-nudge",
        help=argparse.SUPPRESS,
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
        help=argparse.SUPPRESS,
        description=(
            "Idempotent. Adds `eval \"$(praxis shell-nudge)\"` to every "
            "existing RC file (zsh and/or bash). Skips files that "
            "already have the line."
        ),
    )
    isn.set_defaults(func=cmd_install_shell_nudge)

    usn = sub.add_parser("uninstall-shell-nudge", help=argparse.SUPPRESS)
    usn.set_defaults(func=cmd_uninstall_shell_nudge)

    ic = sub.add_parser(
        "install-coach",
        help="Install the Praxis coaching hooks into your AI coding tools.",
        description=(
            "Detect Claude Code / Codex / Copilot and prompt to install "
            "the SessionStart + Stop coaching hooks for each one. The "
            "detection criteria are ~/.claude/settings.json or "
            "~/.claude/projects/ (Claude Code), ~/.codex/ (Codex), and "
            "any VS Code workspace storage with Copilot chat artifacts "
            "(Copilot)."
        ),
    )
    ic.add_argument(
        "--yes", action="store_true",
        help="Assume yes for every prompt (useful in scripted installs).",
    )
    ic_scope = ic.add_mutually_exclusive_group()
    ic_scope.add_argument(
        "--tool", type=str, default=None, metavar="NAME",
        help=(
            "Restrict to a single tool (claude-code, codex, copilot). "
            "Still prompts unless --yes is also set."
        ),
    )
    ic_scope.add_argument(
        "--all", action="store_true",
        help="Iterate every known tool regardless of detection.",
    )
    ic.set_defaults(func=cmd_install_coach)

    uc = sub.add_parser(
        "uninstall-coach",
        help="Remove the Praxis coaching hooks from your AI coding tools.",
        description=(
            "Remove only blocks/entries carrying the _praxisManaged "
            "sentinel (Claude Code, Codex) or bounded by the "
            "praxisManaged markdown markers (Copilot). User-authored "
            "content at the same event names is preserved. On a system "
            "where Praxis was never installed, prints 'Nothing to "
            "uninstall.' and exits 0 without any file writes."
        ),
    )
    uc.add_argument(
        "--yes", action="store_true",
        help="Assume yes for every prompt (useful in scripted uninstalls).",
    )
    uc_scope = uc.add_mutually_exclusive_group()
    uc_scope.add_argument(
        "--tool", type=str, default=None, metavar="NAME",
        help=(
            "Restrict to a single tool (claude-code, codex, copilot). "
            "Still prompts unless --yes is also set."
        ),
    )
    uc_scope.add_argument(
        "--all", action="store_true",
        help="Iterate every known tool regardless of detection.",
    )
    uc.set_defaults(func=cmd_uninstall_coach)

    cfg = sub.add_parser("config", help=argparse.SUPPRESS)
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
    # parse_args raises SystemExit on --help / bad args; let that pass through.
    args = parser.parse_args(argv)
    if getattr(args, "debug", False):
        os.environ["PRAXIS_DEBUG"] = "1"
    # Backstop so a real user never sees a raw traceback. Individual commands
    # still handle their own expected errors and return specific exit codes;
    # this only catches the unexpected. SystemExit (argparse, explicit exits)
    # is not an Exception subclass, so it propagates untouched.
    import sqlite3
    try:
        ensure_config_file()
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except sqlite3.OperationalError as exc:
        if os.environ.get("PRAXIS_DEBUG"):
            raise
        if "locked" in str(exc).lower():
            print(
                "praxis: the profile database is locked - another praxis "
                "process (or the menu-bar app) may be writing. Try again in a "
                "moment.",
                file=sys.stderr,
            )
        else:
            print(f"praxis: database error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as exc:
        if os.environ.get("PRAXIS_DEBUG"):
            raise
        db = resolve_home() / "profile.db"
        print(
            f"praxis: the profile database at {db} looks corrupt or "
            f"unreadable ({exc}). A timestamped backup may sit beside it "
            "(profile.db.backup-*); `praxis doctor` can check.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("PRAXIS_DEBUG"):
            raise
        cmd = getattr(args, "cmd", None) or "command"
        print(f"praxis {cmd}: unexpected error: {exc}", file=sys.stderr)
        print(
            "  This is a bug. Re-run with PRAXIS_DEBUG=1 to see the full "
            "traceback.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
