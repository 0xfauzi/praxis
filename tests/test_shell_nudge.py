"""Tests for the shell-startup reminder snippet and install/uninstall helpers."""

from __future__ import annotations

from pathlib import Path

from praxis.cli.shell_nudge import (
    PRAXIS_NUDGE_COMMENT,
    PRAXIS_NUDGE_LINE,
    emit_snippet,
    install_into_all,
    install_into_rc,
    rc_candidates,
    uninstall_from_all,
    uninstall_from_rc,
)


def test_emit_snippet_contains_function_definition_and_call():
    """The emitted snippet defines __praxis_nudge and calls it once.

    Tests that consumers of `eval "$(praxis shell-nudge)"` get a runnable
    function plus its invocation so the reminder fires on shell startup.
    """
    out = emit_snippet()
    assert "__praxis_nudge()" in out
    # The function must be called (not just defined) -- otherwise the
    # eval is silent.
    assert out.rstrip().endswith("__praxis_nudge")


def test_emit_snippet_delegates_to_praxis_nudge_text(tmp_path: Path):
    """Per US-019 AC #1, the snippet calls `praxis nudge --format text`.

    The literal command string must appear verbatim so the same throttle
    file (~/.praxis/.last_nudge) gates both this snippet and the
    SessionStart hooks documented in PLAN section 5. The unused
    ``tmp_path`` parameter is here for symmetry with the other tests in
    this file; the assertion is purely about the emitted string.
    """
    del tmp_path
    assert "praxis nudge --format text" in emit_snippet()


def test_emit_snippet_guards_against_missing_praxis():
    """Per US-019 AC #3, the snippet fails closed when `praxis` is missing.

    A `command -v praxis >/dev/null 2>&1 || return 0` guard means the
    user never sees ``command not found: praxis`` printed on every shell
    startup. We assert ``command -v`` specifically (not ``which`` /
    ``type``) because it is the only POSIX-portable form.
    """
    out = emit_snippet()
    assert "command -v praxis" in out
    # The guard must short-circuit with `return 0`, not an exit / error.
    assert "command -v praxis >/dev/null 2>&1 || return 0" in out


def test_emit_snippet_swallows_praxis_nudge_errors():
    """A non-zero exit from `praxis nudge` must not surface to the user.

    AC US-019 #3 ("fails closed") plus general defensive hygiene: an
    argparse error, a broken install, or the multi-row invariant
    (`exit 4`) all need to stay invisible. We use ``2>/dev/null`` for
    stderr suppression plus ``|| return 0`` belt-and-braces.
    """
    out = emit_snippet()
    assert "2>/dev/null" in out
    # The nudge invocation specifically must have || return 0 guarding
    # against non-zero exit; not just the command -v guard.
    assert "praxis nudge --format text 2>/dev/null || return 0" in out


def test_emit_snippet_uses_portable_posix_shell():
    """The snippet must work in zsh, bash, and dash without bashisms."""
    snippet = emit_snippet()
    # No double-brackets (`[[ ... ]]` is bash/zsh-only, not dash/sh).
    assert "[[ " not in snippet
    # No `which` (alias-dependent; `command -v` is POSIX).
    assert "which praxis" not in snippet


def test_emit_snippet_does_not_reference_legacy_digest_paths():
    """The legacy mtime/marker logic must be gone (US-019).

    The pre-US-019 snippet hard-coded ``$HOME/.praxis/latest.html`` and
    ``$HOME/.praxis/.last_opened`` checks; that logic now lives inside
    ``praxis nudge``. Asserting the legacy markers are absent guards
    against accidental revert during future refactors.
    """
    out = emit_snippet()
    assert "latest.html" not in out
    assert ".last_opened" not in out
    assert "-mtime" not in out


def test_rc_candidates_returns_zshrc_and_bashrc(tmp_path: Path):
    """zsh comes first; bash second. Both should appear."""
    candidates = rc_candidates(tmp_path)
    assert candidates == [tmp_path / ".zshrc", tmp_path / ".bashrc"]


def test_install_into_rc_appends_line_when_missing(tmp_path: Path):
    rc = tmp_path / ".zshrc"
    rc.write_text("export PATH=/usr/local/bin\n", encoding="utf-8")
    assert install_into_rc(rc) is True
    body = rc.read_text(encoding="utf-8")
    assert PRAXIS_NUDGE_LINE in body
    assert PRAXIS_NUDGE_COMMENT in body
    # Original content preserved.
    assert "export PATH=/usr/local/bin" in body


def test_install_into_rc_is_idempotent(tmp_path: Path):
    rc = tmp_path / ".zshrc"
    rc.write_text("alias l='ls -la'\n", encoding="utf-8")
    install_into_rc(rc)
    # Second call: file should not change.
    before = rc.read_text(encoding="utf-8")
    assert install_into_rc(rc) is False
    after = rc.read_text(encoding="utf-8")
    assert before == after
    # And only one nudge line is present.
    assert after.count(PRAXIS_NUDGE_LINE) == 1


def test_install_into_rc_returns_false_when_rc_missing(tmp_path: Path):
    """We don't create RC files from thin air -- a missing file is a no-op."""
    missing = tmp_path / "no-such-file"
    assert install_into_rc(missing) is False
    assert not missing.exists()


def test_uninstall_from_rc_removes_line_and_comment(tmp_path: Path):
    rc = tmp_path / ".zshrc"
    rc.write_text("alias l='ls -la'\n", encoding="utf-8")
    install_into_rc(rc)
    assert uninstall_from_rc(rc) is True
    body = rc.read_text(encoding="utf-8")
    assert PRAXIS_NUDGE_LINE not in body
    assert PRAXIS_NUDGE_COMMENT not in body
    # Surrounding content preserved.
    assert "alias l='ls -la'" in body


def test_uninstall_from_rc_preserves_surrounding_content(tmp_path: Path):
    """Lines before and after the nudge block must survive untouched."""
    rc = tmp_path / ".zshrc"
    rc.write_text(
        f"export EDITOR=nvim\n\n{PRAXIS_NUDGE_COMMENT}\n{PRAXIS_NUDGE_LINE}\n\nalias ll='ls -la'\n",
        encoding="utf-8",
    )
    uninstall_from_rc(rc)
    body = rc.read_text(encoding="utf-8")
    assert "export EDITOR=nvim" in body
    assert "alias ll='ls -la'" in body
    assert PRAXIS_NUDGE_LINE not in body


def test_uninstall_from_rc_returns_false_when_line_absent(tmp_path: Path):
    rc = tmp_path / ".zshrc"
    rc.write_text("export FOO=bar\n", encoding="utf-8")
    assert uninstall_from_rc(rc) is False
    assert rc.read_text(encoding="utf-8") == "export FOO=bar\n"


def test_install_into_all_skips_missing_files(tmp_path: Path):
    """Only RCs that actually exist on disk get the nudge."""
    (tmp_path / ".zshrc").write_text("# zsh user\n", encoding="utf-8")
    # .bashrc deliberately absent.
    modified = install_into_all(tmp_path)
    assert modified == [tmp_path / ".zshrc"]


def test_install_into_all_returns_empty_when_nothing_to_do(tmp_path: Path):
    """No RC files at all -> empty modified list, no errors."""
    assert install_into_all(tmp_path) == []


def test_install_then_uninstall_restores_clean_state(tmp_path: Path):
    """A round-trip leaves the RC file functionally identical."""
    rc = tmp_path / ".zshrc"
    original = "export PATH=/usr/local/bin\nalias l='ls -la'\n"
    rc.write_text(original, encoding="utf-8")
    install_into_rc(rc)
    uninstall_from_rc(rc)
    final = rc.read_text(encoding="utf-8")
    # The original content is intact (modulo trailing-blank normalisation).
    assert "export PATH=/usr/local/bin" in final
    assert "alias l='ls -la'" in final
    assert PRAXIS_NUDGE_LINE not in final
    assert PRAXIS_NUDGE_COMMENT not in final


def test_uninstall_from_all_returns_only_files_actually_modified(tmp_path: Path):
    (tmp_path / ".zshrc").write_text("export A=1\n", encoding="utf-8")
    (tmp_path / ".bashrc").write_text("export B=2\n", encoding="utf-8")
    install_into_all(tmp_path)
    modified = uninstall_from_all(tmp_path)
    assert set(modified) == {tmp_path / ".zshrc", tmp_path / ".bashrc"}
    # Second uninstall is a no-op.
    assert uninstall_from_all(tmp_path) == []
