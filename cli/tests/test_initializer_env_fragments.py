"""Tests for the Phase 4 VM-side env-and-secrets fragments.

Pins the wire shape of the four init-time writers that deploy the
runtime contract for SSH SetEnv + sudoers env_keep + identity profile
fragments. See ``docs/adrs/0014-sshd-accept-env-wildcard.md`` and the
env-and-secrets SDD plan Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentworks.vms.initializer import (
    AGENTWORKS_PROFILE,
    AGENTWORKS_RC,
    AGENTWORKS_SSHD_ACCEPT_ENV_PATH,
    AGENTWORKS_SUDOERS_ENV_KEEP_PATH,
    _ensure_agentworks_files_sourced,
    _write_agentworks_identity_profile,
    _write_agentworks_profile,
    _write_sshd_accept_env,
    _write_sudoers_env_keep,
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
            return _SpyResult(
                ok=self._validate_ok, returncode=0 if self._validate_ok else 1
            )
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
# Identity profile fragment
# ---------------------------------------------------------------------------


def test_identity_profile_writes_system_wide_file() -> None:
    target = _SpyTarget()
    _write_agentworks_identity_profile(
        target,
        {"AGENTWORKS_VM": "vm-1", "AGENTWORKS_PLATFORM": "lima"},
        _SpyLogger(),
    )
    profile_writes = [r for r, _ in target.runs if "/etc/profile.d/agentworks-identity.sh" in r]
    assert len(profile_writes) == 1
    assert "tee /etc/profile.d/agentworks-identity.sh" in profile_writes[0]
    assert "export AGENTWORKS_VM=vm-1" in profile_writes[0]
    assert "export AGENTWORKS_PLATFORM=lima" in profile_writes[0]


def test_identity_profile_mirrors_into_zprofile() -> None:
    # ``prior_file_present=True`` so the strip-and-rewrite path runs.
    target = _SpyTarget(prior_file_present=True)
    _write_agentworks_identity_profile(
        target,
        {"AGENTWORKS_VM": "vm-1"},
        _SpyLogger(),
    )
    zsh_writes = [r for r, _ in target.runs if "/etc/zsh/zprofile" in r]
    # Three zprofile touches on the reinit-with-prior-block path: the grep
    # checks for the markers, the sed strip, then the append.
    assert any("sed -i" in r for r in zsh_writes)
    assert any("tee -a /etc/zsh/zprofile" in r for r in zsh_writes)


def test_identity_profile_strips_prior_block_before_appending() -> None:
    """Reinit safety: the agentworks-identity block in /etc/zsh/zprofile is
    bracketed by begin/end markers and stripped via sed before the new block
    appends, so reinit doesn't accumulate stale entries."""
    target = _SpyTarget(prior_file_present=True)
    _write_agentworks_identity_profile(
        target, {"AGENTWORKS_VM": "vm-1"}, _SpyLogger(),
    )
    commands = [r for r, _ in target.runs]
    sed_idx = next(i for i, c in enumerate(commands) if "sed -i" in c and "zprofile" in c)
    append_idx = next(i for i, c in enumerate(commands) if "tee -a /etc/zsh/zprofile" in c)
    assert sed_idx < append_idx
    # Marker shape is the same on strip and on write.
    assert "agentworks-identity-begin" in commands[sed_idx]


def test_identity_profile_skips_strip_on_first_init() -> None:
    """When /etc/zsh/zprofile doesn't exist yet (first init on a fresh VM),
    the helper skips the strip step and just appends the new block. The
    ``tee -a`` creates the file."""
    target = _SpyTarget(prior_file_present=False)
    _write_agentworks_identity_profile(
        target, {"AGENTWORKS_VM": "vm-1"}, _SpyLogger(),
    )
    commands = [r for r, _ in target.runs]
    assert not any("sed -i" in c and "zprofile" in c for c in commands)
    # The append still happens unconditionally.
    assert any("tee -a /etc/zsh/zprofile" in c for c in commands)


def test_identity_profile_creates_etc_zsh_directory() -> None:
    """Regression: on a fresh VM, /etc/zsh/ doesn't exist until apt
    installs zsh-common (which happens later in init than the identity-
    profile write). The mirror previously failed with ``tee: /etc/zsh/
    zprofile: No such file or directory``. The helper now runs
    ``mkdir -p /etc/zsh`` before the append so the write succeeds even
    when zsh isn't installed yet."""
    target = _SpyTarget(prior_file_present=False)
    _write_agentworks_identity_profile(
        target, {"AGENTWORKS_VM": "vm-1"}, _SpyLogger(),
    )
    commands = [r for r, _ in target.runs]
    mkdir_idx = next(
        (i for i, c in enumerate(commands) if "mkdir -p /etc/zsh" in c),
        None,
    )
    assert mkdir_idx is not None, (
        "expected `mkdir -p /etc/zsh` somewhere in the run; got: "
        f"{commands}"
    )
    append_idx = next(
        i for i, c in enumerate(commands) if "tee -a /etc/zsh/zprofile" in c
    )
    assert mkdir_idx < append_idx, "mkdir must precede the append"


def test_identity_profile_only_strips_when_both_markers_present() -> None:
    """Reinit safety: if only one of begin/end markers exists in zprofile
    (a half-edited file), the helper skips the strip step and warns rather
    than running sed with an unmatched address range that could nuke
    operator content."""

    class _HalfEditedTarget(_SpyTarget):
        def run(self, command: str, **kwargs: object) -> _SpyResult:
            self.runs.append((command, kwargs))
            # File exists.
            if command.startswith("sudo test -f "):
                return _SpyResult(ok=True)
            # Begin marker present, end marker missing.
            if "grep -qF" in command and "begin" in command:
                return _SpyResult(ok=True)
            if "grep -qF" in command and "end" in command:
                return _SpyResult(ok=False, returncode=1)
            return _SpyResult(ok=True)

    target = _HalfEditedTarget()
    _write_agentworks_identity_profile(
        target, {"AGENTWORKS_VM": "vm-1"}, _SpyLogger(),
    )
    commands = [r for r, _ in target.runs]
    # No sed -i call: the strip is gated on BOTH markers being present.
    assert not any("sed -i" in c and "zprofile" in c for c in commands)
    # The fresh block is still appended.
    assert any("tee -a /etc/zsh/zprofile" in c for c in commands)


def test_identity_profile_quotes_values_with_special_chars() -> None:
    target = _SpyTarget()
    _write_agentworks_identity_profile(
        target,
        {"AGENTWORKS_VM_HOST": "my host"},
        _SpyLogger(),
    )
    profile_write = next(r for r, _ in target.runs if "agentworks-identity.sh" in r)
    assert "'my host'" in profile_write


# ---------------------------------------------------------------------------
# sshd AcceptEnv
# ---------------------------------------------------------------------------


def test_sshd_accept_env_writes_config_file() -> None:
    target = _SpyTarget()
    _write_sshd_accept_env(target, _SpyLogger())
    config_writes = [r for r, _ in target.runs if AGENTWORKS_SSHD_ACCEPT_ENV_PATH in r and "tee" in r]
    assert len(config_writes) == 1
    assert "AcceptEnv *" in config_writes[0]


def test_sshd_accept_env_validates_before_reload() -> None:
    """sshd -t must run BEFORE systemctl reload; if the validation fails the
    reload doesn't happen (and the prior config stays active)."""
    target = _SpyTarget()
    _write_sshd_accept_env(target, _SpyLogger())
    commands = [r for r, _ in target.runs]
    validate_idx = next(i for i, c in enumerate(commands) if "sshd -t" in c)
    reload_idx = next(i for i, c in enumerate(commands) if "systemctl reload ssh" in c)
    assert validate_idx < reload_idx


def test_sshd_accept_env_restores_prior_file_on_validation_failure() -> None:
    """When ``sshd -t`` rejects the new file, the prior file is restored
    from .bak and the systemctl reload does NOT run."""
    target = _SpyTarget(validate_ok=False, prior_file_present=True)
    logger = _SpyLogger()
    _write_sshd_accept_env(target, logger)
    commands = [r for r, _ in target.runs]
    # Prior file was backed up before the new write...
    assert any("cp" in c and ".bak" in c for c in commands)
    # ...then restored after validate failed.
    assert any("mv" in c and ".bak" in c for c in commands)
    # The reload MUST NOT run; broken sshd config never becomes active.
    assert not any("systemctl reload ssh" in c for c in commands)
    # Operator gets a warning pointing at recovery.
    assert any("reinit" in w.lower() for w in logger.warnings)


def test_sshd_accept_env_removes_file_when_no_prior_and_validation_fails() -> None:
    """No prior file + validation fails: the new (broken) file is removed
    so /etc/ssh/sshd_config.d/ doesn't accumulate non-validated content."""
    target = _SpyTarget(validate_ok=False, prior_file_present=False)
    _write_sshd_accept_env(target, _SpyLogger())
    commands = [r for r, _ in target.runs]
    assert any("rm -f" in c and "agentworks-accept-env" in c for c in commands)
    assert not any("systemctl reload ssh" in c for c in commands)


def test_sshd_accept_env_success_with_prior_file_ordering() -> None:
    """When a prior config file exists AND validation succeeds, the
    sequence is cp(backup) -> tee(write) -> sshd -t -> rm -f .bak ->
    systemctl reload ssh. Pins the order so a future refactor that
    reorders steps fails loudly (e.g. swapping reload before validate
    would activate an untested config)."""
    target = _SpyTarget(validate_ok=True, prior_file_present=True)
    _write_sshd_accept_env(target, _SpyLogger())
    commands = [r for r, _ in target.runs]

    cp_idx = next(i for i, c in enumerate(commands) if c.startswith("sudo cp ") and ".bak" in c)
    tee_idx = next(
        i for i, c in enumerate(commands) if "tee" in c and "50-agentworks-accept-env" in c
    )
    validate_idx = next(i for i, c in enumerate(commands) if "sshd -t" in c)
    cleanup_idx = next(
        i for i, c in enumerate(commands) if c.startswith("sudo rm -f ") and ".bak" in c
    )
    reload_idx = next(i for i, c in enumerate(commands) if "systemctl reload ssh" in c)

    assert cp_idx < tee_idx < validate_idx < cleanup_idx < reload_idx


# ---------------------------------------------------------------------------
# sudoers env_keep
# ---------------------------------------------------------------------------


def test_sudoers_env_keep_writes_and_validates() -> None:
    target = _SpyTarget()
    _write_sudoers_env_keep(target, _SpyLogger())
    commands = [r for r, _ in target.runs]
    # Body is written via tee to the staging path.
    staging = AGENTWORKS_SUDOERS_ENV_KEEP_PATH + ".tmp"
    assert any(f"tee {staging}" in c for c in commands)
    # visudo -cf validates the staging file.
    assert any(f"visudo -cf '{staging}'" in c or f"visudo -cf {staging}" in c for c in commands)
    # mv promotes staging to the real path AFTER validation.
    mv_idx = next(i for i, c in enumerate(commands) if "mv" in c and "50-agentworks-env-keep" in c)
    validate_idx = next(i for i, c in enumerate(commands) if "visudo -cf" in c)
    assert validate_idx < mv_idx


def test_sudoers_env_keep_rejects_on_visudo_failure() -> None:
    """If visudo -cf rejects the fragment, the staging file is removed and
    the helper warns; the real sudoers.d/ file is not touched."""
    target = _SpyTarget(validate_ok=False)
    logger = _SpyLogger()
    _write_sudoers_env_keep(target, logger)
    commands = [r for r, _ in target.runs]
    # The staging file got removed; the real path was NOT mv-ed.
    assert any("rm -f" in c and "50-agentworks-env-keep" in c for c in commands)
    assert not any(
        f"mv '{AGENTWORKS_SUDOERS_ENV_KEEP_PATH}.tmp' '{AGENTWORKS_SUDOERS_ENV_KEEP_PATH}'" in c
        for c in commands
    )
    # The helper warned (non-fatal).
    assert any("visudo" in w for w in logger.warnings)


def test_sudoers_env_keep_includes_agentworks_and_aw_patterns() -> None:
    target = _SpyTarget()
    _write_sudoers_env_keep(target, _SpyLogger())
    tee_cmd = next(r for r, _ in target.runs if "tee" in r and "50-agentworks-env-keep" in r)
    assert 'env_keep += "AGENTWORKS_* AW_*"' in tee_cmd


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
        target, home="/home/me", shell="bash", logger=_SpyLogger(),
    )
    commands = [c for c, _ in target.runs]
    profile_appends = [
        c for c in commands if AGENTWORKS_PROFILE in c and "grep -q" in c
    ]
    assert any("/home/me/.profile" in c for c in profile_appends)
    assert any("/home/me/.bashrc" in c for c in profile_appends)
    assert not any(".zprofile" in c for c in profile_appends), (
        "bash shell should not touch .zprofile"
    )


def test_ensure_files_sourced_adds_zprofile_when_shell_is_zsh() -> None:
    target = _SpyTarget()
    _ensure_agentworks_files_sourced(
        target, home="/home/me", shell="zsh", logger=_SpyLogger(),
    )
    commands = [c for c, _ in target.runs]
    profile_appends = [
        c for c in commands if AGENTWORKS_PROFILE in c and "grep -q" in c
    ]
    assert any("/home/me/.zprofile" in c for c in profile_appends)


def test_ensure_files_sourced_appends_rc_to_interactive_rcs() -> None:
    """Similar check for AGENTWORKS_RC (mise activation) -- always
    .bashrc, plus .zshrc when shell=zsh."""
    target = _SpyTarget()
    _ensure_agentworks_files_sourced(
        target, home="/home/me", shell="zsh", logger=_SpyLogger(),
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
        target, home="/home/me", shell="bash", logger=_SpyLogger(),
    )
    for command, _ in target.runs:
        assert command.startswith("grep -q "), (
            f"expected grep-or-append shape; got: {command}"
        )
        assert " || printf " in command


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
