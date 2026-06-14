"""Tests for the Phase 4 VM-side env-and-secrets fragments.

Pins the wire shape of the four init-time writers that deploy the
runtime contract for SSH SetEnv + sudoers env_keep + identity profile
fragments. See ``new-adrs/sshd-accept-env-wildcard.md`` and the
env-and-secrets SDD plan Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentworks.vms.initializer import (
    AGENTWORKS_PROFILE,
    AGENTWORKS_SSHD_ACCEPT_ENV_PATH,
    AGENTWORKS_SUDOERS_ENV_KEEP_PATH,
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
    """ExecTarget-shaped stub that records run + write_file calls."""

    def __init__(self, *, validate_ok: bool = True) -> None:
        self.runs: list[tuple[str, dict[str, object]]] = []
        self.writes: list[tuple[str, str]] = []
        self._validate_ok = validate_ok

    def run(self, command: str, **kwargs: object) -> _SpyResult:
        self.runs.append((command, kwargs))
        # ``visudo -cf`` validation: switchable via constructor.
        if "visudo -cf" in command:
            return _SpyResult(ok=self._validate_ok, returncode=0 if self._validate_ok else 1)
        # ``sshd -t`` validation: assume OK unless overridden.
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
    target = _SpyTarget()
    _write_agentworks_identity_profile(
        target,
        {"AGENTWORKS_VM": "vm-1"},
        _SpyLogger(),
    )
    zsh_writes = [r for r, _ in target.runs if "/etc/zsh/zprofile" in r]
    # Two zprofile touches: the sed-clear of any prior block, plus the append.
    assert any("sed -i" in r for r in zsh_writes)
    assert any("tee -a /etc/zsh/zprofile" in r for r in zsh_writes)


def test_identity_profile_strips_prior_block_before_appending() -> None:
    """Reinit safety: the agentworks-identity block in /etc/zsh/zprofile is
    bracketed by begin/end markers and stripped via sed before the new block
    appends, so reinit doesn't accumulate stale entries."""
    target = _SpyTarget()
    _write_agentworks_identity_profile(
        target, {"AGENTWORKS_VM": "vm-1"}, _SpyLogger(),
    )
    commands = [r for r, _ in target.runs]
    sed_idx = next(i for i, c in enumerate(commands) if "sed -i" in c and "zprofile" in c)
    append_idx = next(i for i, c in enumerate(commands) if "tee -a /etc/zsh/zprofile" in c)
    assert sed_idx < append_idx
    # Marker shape is the same on strip and on write.
    assert "agentworks-identity-begin" in commands[sed_idx]


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
# Per-user profile fragment (AGENTWORKS_USER)
# ---------------------------------------------------------------------------


def test_per_user_profile_writes_agentworks_user_when_provided() -> None:
    target = _SpyTarget()
    _write_agentworks_profile(
        target,
        path_additions=[],
        logger=_SpyLogger(),
        identity_env={"AGENTWORKS_USER": "aw-claude"},
    )
    profile_writes = [c for p, c in target.writes if AGENTWORKS_PROFILE in p]
    assert len(profile_writes) == 1
    assert "export AGENTWORKS_USER=aw-claude" in profile_writes[0]


def test_per_user_profile_omits_identity_when_not_provided() -> None:
    """Backward-compat: existing callers that don't pass identity_env get the
    old shape (just PATH exports)."""
    target = _SpyTarget()
    _write_agentworks_profile(
        target,
        path_additions=["/opt/bin"],
        logger=_SpyLogger(),
    )
    profile_writes = [c for p, c in target.writes if AGENTWORKS_PROFILE in p]
    assert len(profile_writes) == 1
    assert "PATH=" in profile_writes[0]
    assert "AGENTWORKS_USER" not in profile_writes[0]


def test_per_user_profile_appends_path_then_identity() -> None:
    target = _SpyTarget()
    _write_agentworks_profile(
        target,
        path_additions=["/opt/bin"],
        logger=_SpyLogger(),
        identity_env={"AGENTWORKS_USER": "agentworks"},
    )
    profile_writes = [c for p, c in target.writes if AGENTWORKS_PROFILE in p]
    assert len(profile_writes) == 1
    body = profile_writes[0]
    # PATH export before AGENTWORKS_USER (so user's shell tweaks to PATH that
    # reference AGENTWORKS_USER, if any, can rely on it).
    path_idx = body.find("PATH=")
    user_idx = body.find("AGENTWORKS_USER")
    assert 0 < path_idx < user_idx


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
