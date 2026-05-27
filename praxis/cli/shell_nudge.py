"""Shell-startup reminder for the weekly digest.

The macOS notification fires once and disappears. If the user misses
it (Focus mode, laptop closed, banner dismissed), nothing else surfaces
the digest -- it sits unread until next week overwrites the symlink in
their attention.

The shell nudge closes that gap: on every interactive shell startup,
print one line if a fresh-but-unread digest is on disk. "Fresh" means
modified in the last 5 days; "unread" means `~/.praxis/.last_opened`
is missing or older than `~/.praxis/latest.html`. `praxis open` is the
canonical reader that touches the marker, so once the user opens the
digest the reminder stops on the next shell.

The snippet is plain bash/zsh and does not invoke Python per shell
startup (perf): the cost on a new shell is one `find`, one stat, and
one printf at most.

Two integration paths:
  * `praxis install-weekly` prompts the user once and appends the
    `eval "$(praxis shell-nudge)"` line to the right RC file(s).
  * `praxis install-shell-nudge` / `praxis uninstall-shell-nudge` let
    the user manage it manually without re-running install-weekly.
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

      * Uses POSIX `[ -f ]` so it works in zsh, bash, dash without
        bashism warnings.
      * `find -mtime -5 -print -quit` is the portable way to ask
        "modified within the last 5 days"; `stat` formats differ
        between BSD (macOS) and GNU (Linux) and would need two
        codepaths.
      * Reminder line goes to stderr so script consumers piping shell
        stdout aren't polluted.
      * The eval is hot-pathed: any failure mode short-circuits with
        `return 0` so a broken nudge can never block the shell.
    """
    return r"""__praxis_nudge() {
  local html="$HOME/.praxis/latest.html"
  local marker="$HOME/.praxis/.last_opened"
  [ -f "$html" ] || return 0
  find "$html" -mtime -5 -print -quit >/dev/null 2>&1 || return 0
  if [ -f "$marker" ] && [ "$marker" -nt "$html" ]; then
    return 0
  fi
  printf 'Praxis: this week'\''s digest is ready. Run `praxis open` to read.\n' >&2
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
    appended = existing.rstrip("\n") + "\n\n" + PRAXIS_NUDGE_COMMENT + "\n" + PRAXIS_NUDGE_LINE + "\n"
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
