"""Tests for the per-user profile fragment and its source lines.

The system fragments (identity profile, sshd, sudoers) are covered over the
file program in ``test_initializer_system_fragments.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentworks.vms.initializer import (
    AGENTWORKS_PROFILE,
    AGENTWORKS_RC,
    _ensure_agentworks_files_sourced,
    _write_agentworks_profile,
)


@dataclass
class _SpyResult:
    ok: bool = True
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _SpyTarget:
    """``Transport``-shaped stub that records run + write_file calls.

    ``validate_ok`` controls the return of ``visudo -cf`` AND ``sshd -t``
    so tests can simulate validation failure for either helper.
    ``prior_file_present`` controls the ``test -f`` probe used by the sshd
    helper to detect existing config (so the backup-restore branch can be
    exercised).
    """

    def __init__(
        self,
        *,
        validate_ok: bool = True,
        prior_file_present: bool = False,
    ) -> None:
        self.runs: list[tuple[str, dict[str, object]]] = []
        self.writes: list[tuple[str, str]] = []
        self._validate_ok = validate_ok
        self._prior_file_present = prior_file_present

    def run(self, command: str, **kwargs: object) -> _SpyResult:
        self.runs.append((command, kwargs))
        if "visudo -cf" in command or "sshd -t" in command:
            return _SpyResult(ok=self._validate_ok, returncode=0 if self._validate_ok else 1)
        if command.startswith("sudo test -f "):
            return _SpyResult(
                ok=self._prior_file_present,
                returncode=0 if self._prior_file_present else 1,
            )
        return _SpyResult(ok=True)

    def write_file(self, path: str, content: str, mode: str = "0644") -> None:  # noqa: ARG002
        self.writes.append((path, content))


class _SpyLogger:
    def __init__(self) -> None:
        self.steps: list[str] = []
        self.warnings: list[str] = []

    def step(self, name: str) -> None:
        self.steps.append(name)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)


# ---------------------------------------------------------------------------
# Per-user profile fragment (PATH additions)
# ---------------------------------------------------------------------------


def test_per_user_profile_writes_path_exports() -> None:
    target = _SpyTarget()
    _write_agentworks_profile(
        target,
        path_additions=["/opt/bin", "~/.local/bin"],
        logger=_SpyLogger(),
    )
    profile_writes = [c for p, c in target.writes if AGENTWORKS_PROFILE in p]
    assert len(profile_writes) == 1
    body = profile_writes[0]
    assert 'export PATH="/opt/bin:$PATH"' in body
    assert 'export PATH="$HOME/.local/bin:$PATH"' in body


def test_per_user_profile_writes_empty_body_when_no_paths() -> None:
    """Reinit safety: an operator who removes their install commands gets
    a clean fragment (just the managed-by header) on the next reinit."""
    target = _SpyTarget()
    _write_agentworks_profile(
        target,
        path_additions=[],
        logger=_SpyLogger(),
    )
    profile_writes = [c for p, c in target.writes if AGENTWORKS_PROFILE in p]
    assert len(profile_writes) == 1
    body = profile_writes[0]
    assert "PATH=" not in body
    assert "Managed by agentworks" in body


# ---------------------------------------------------------------------------
# Defensive end-of-setup ensure step
# ---------------------------------------------------------------------------


def test_ensure_files_sourced_appends_profile_to_bash_rcs() -> None:
    """For shell=bash, the ensure step appends the profile source line to
    ~/.profile and ~/.bashrc (not ~/.zprofile). Idempotent grep-or-append
    shape."""
    target = _SpyTarget()
    _ensure_agentworks_files_sourced(
        target,
        home="/home/me",
        shell="bash",
        logger=_SpyLogger(),
    )
    commands = [c for c, _ in target.runs]
    profile_appends = [c for c in commands if AGENTWORKS_PROFILE in c and "grep -q" in c]
    assert any("/home/me/.profile" in c for c in profile_appends)
    assert any("/home/me/.bashrc" in c for c in profile_appends)
    assert not any(".zprofile" in c for c in profile_appends), "bash shell should not touch .zprofile"


def test_ensure_files_sourced_adds_zprofile_when_shell_is_zsh() -> None:
    target = _SpyTarget()
    _ensure_agentworks_files_sourced(
        target,
        home="/home/me",
        shell="zsh",
        logger=_SpyLogger(),
    )
    commands = [c for c, _ in target.runs]
    profile_appends = [c for c in commands if AGENTWORKS_PROFILE in c and "grep -q" in c]
    assert any("/home/me/.zprofile" in c for c in profile_appends)


def test_ensure_files_sourced_appends_rc_to_interactive_rcs() -> None:
    """Similar check for AGENTWORKS_RC (mise activation) -- always
    .bashrc, plus .zshrc when shell=zsh."""
    target = _SpyTarget()
    _ensure_agentworks_files_sourced(
        target,
        home="/home/me",
        shell="zsh",
        logger=_SpyLogger(),
    )
    commands = [c for c, _ in target.runs]
    rc_appends = [c for c in commands if AGENTWORKS_RC in c and "grep -q" in c]
    assert any("/home/me/.bashrc" in c for c in rc_appends)
    assert any("/home/me/.zshrc" in c for c in rc_appends)


def test_ensure_files_sourced_uses_grep_or_append_shape() -> None:
    """The source-line append is idempotent: ``grep -q ... || printf ... >> rc``.
    A future contributor swapping for an unconditional append would
    introduce duplicate source lines on every reinit."""
    target = _SpyTarget()
    _ensure_agentworks_files_sourced(
        target,
        home="/home/me",
        shell="bash",
        logger=_SpyLogger(),
    )
    for command, _ in target.runs:
        assert command.startswith("grep -q "), f"expected grep-or-append shape; got: {command}"
        assert " || printf " in command


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
