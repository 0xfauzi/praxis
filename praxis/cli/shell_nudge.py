"""Shell-startup reminder for the active commitment (US-019).

The macOS notification fires once and disappears. If the user misses
it (Focus mode, laptop closed, banner dismissed), nothing else surfaces
the active commitment -- it sits unseen until next week.

The shell nudge closes that gap: on every interactive shell startup,
delegate to ``praxis nudge --format text``. That command is the single
source of truth (PLAN section 5: "One source of truth, three surfaces")
for what to show -- it reads the active follow_ups row, gates output
through the shared ``~/.praxis/.last_nudge`` throttle file, and prints
one line ``[Praxis] This week: <commitment>`` only when a fresh fire is
warranted. A shell startup that follows a Claude Code or Codex
SessionStart fire within ``[nudge].throttle_minutes`` (default 30) sees
no output because the throttle file is shared across surfaces.

The snippet stays POSIX (bash/zsh/dash) and fails closed: if ``praxis``
is missing from PATH, or if it raises for any reason, the user prompt
gets nothing -- never a "command not found" or a Python traceback.

Two integration paths:
  * ``praxis install-weekly`` prompts the user once and appends the
    ``eval "$(praxis shell-nudge)"`` line to the right RC file(s).
  * ``praxis install-shell-nudge`` / ``praxis uninstall-shell-nudge``
    let the user manage it manually without re-running install-weekly.
"""

from __future__ import annotations

from pathlib import Path

# The literal one-liner that gets appended to ~/.zshrc / ~/.bashrc. Kept
# as a module constant so install + uninstall + idempotency checks all
# match on the same string. Don't reword without updating the uninstall
# matcher.
PRAXIS_NUDGE_LINE: str = 'eval "$(praxis shell-nudge)"'

# The wrapping comment lines we add around the eval so an uninstall can
# also strip the visual block. The comment line on its own is matched
# loosely (startswith) so a future relocation of the eval line doesn't
# orphan the marker.
PRAXIS_NUDGE_COMMENT: str = "# praxis weekly digest reminder"


def emit_snippet() -> str:
    """Return the shell snippet that `eval "$(praxis shell-nudge)"` consumes.

    The snippet defines and calls a tiny function. Implementation notes:

      * Uses POSIX ``command -v`` (not bashism ``which`` or ``type``)
        so it works in zsh, bash, dash without warnings.
      * Delegates the actual cue-vs-no-cue decision to
        ``praxis nudge --format text``. That command shares the
        ``~/.praxis/.last_nudge`` throttle file with the Claude Code /
        Codex SessionStart hooks, so a shell startup within
        ``[nudge].throttle_minutes`` of either hook firing is silent.
      * ``praxis nudge`` ``2>/dev/null`` so any error output (an
        argparse complaint, a Python traceback from a broken install,
        an unreadable config) never lands on the user's prompt. The
        ``|| return 0`` adds a belt-and-braces guard so a non-zero
        exit (e.g. exit 4 from the multi-row invariant) also stays
        invisible.
      * The PATH guard ``command -v praxis >/dev/null 2>&1 || return 0``
        means a system where ``praxis`` has been uninstalled (or never
        installed -- e.g. the RC line lingered after a reinstall) just
        no-ops; the user does not see ``command not found: praxis``
        on every shell startup.
    """
    return r"""__praxis_nudge() {
  command -v praxis >/dev/null 2>&1 || return 0
  praxis nudge --format text 2>/dev/null || return 0
}
__praxis_nudge
"""


def rc_candidates(home: Path | None = None) -> list[Path]:
    """Return the RC files we'd consider appending the nudge line to.

    Order matters for the install loop: we install into every file in
    the returned order so a user with both zsh AND bash gets the
    reminder regardless of which shell they actually open.

    We do NOT create the file if it doesn't exist -- installing into a
    nonexistent ~/.bashrc on a zsh-only machine would be confusing and
    unhelpful. The caller filters on `.exists()`.
    """
    base = home if home is not None else Path.home()
    return [base / ".zshrc", base / ".bashrc"]


def install_into_rc(rc_path: Path) -> bool:
    """Append the nudge eval line to `rc_path` idempotently.

    Returns True when the file changed (line was added), False when the
    line was already present. Creates the file's parent dir if needed
    but does NOT create the file from thin air -- if the RC file
    doesn't exist this returns False, leaving the decision to the
    caller (typically install logs "skipped: ~/.zshrc does not exist").
    """
    if not rc_path.exists():
        return False
    existing = rc_path.read_text(encoding="utf-8")
    if PRAXIS_NUDGE_LINE in existing:
        return False
    # Append with a leading blank line and the comment marker so a
    # later `tail -5` reads cleanly and an uninstall has an anchor.
    appended = (
        existing.rstrip("\n") + "\n\n" + PRAXIS_NUDGE_COMMENT + "\n" + PRAXIS_NUDGE_LINE + "\n"
    )
    rc_path.write_text(appended, encoding="utf-8")
    return True


def uninstall_from_rc(rc_path: Path) -> bool:
    """Remove the nudge eval line (and its comment marker) from `rc_path`.

    Returns True when something was removed, False when there was
    nothing to remove. Preserves all surrounding content -- only the
    two known lines are stripped, plus a single trailing blank line if
    the removal leaves consecutive blanks.
    """
    if not rc_path.exists():
        return False
    text = rc_path.read_text(encoding="utf-8")
    if PRAXIS_NUDGE_LINE not in text:
        return False
    out_lines: list[str] = []
    removed = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == PRAXIS_NUDGE_LINE:
            removed = True
            continue
        if stripped.startswith(PRAXIS_NUDGE_COMMENT):
            # Drop the comment marker too; it was added by us.
            removed = True
            continue
        out_lines.append(line)
    # Collapse the (now-orphan) blank line that sat before the marker.
    cleaned: list[str] = []
    for line in out_lines:
        if cleaned and cleaned[-1] == "" and line == "":
            continue
        cleaned.append(line)
    new_text = "\n".join(cleaned)
    if not new_text.endswith("\n"):
        new_text += "\n"
    rc_path.write_text(new_text, encoding="utf-8")
    return removed


def install_into_all(home: Path | None = None) -> list[Path]:
    """Install the nudge into every existing RC candidate.

    Returns the list of files actually modified (i.e. excludes RC files
    that didn't exist on disk and ones where the line was already
    present).
    """
    modified: list[Path] = []
    for rc in rc_candidates(home):
        if install_into_rc(rc):
            modified.append(rc)
    return modified


def uninstall_from_all(home: Path | None = None) -> list[Path]:
    """Uninstall the nudge from every RC candidate that still contains it."""
    modified: list[Path] = []
    for rc in rc_candidates(home):
        if uninstall_from_rc(rc):
            modified.append(rc)
    return modified
