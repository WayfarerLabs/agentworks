"""Tests for the env-and-secrets doctor health groups (FRD R6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.config import load_config
from agentworks.doctor import Status, _check_env, _check_secrets


def _write_config(tmp_path: Path, *, extras: str = "") -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""\
[operator]
ssh_public_key = "{pub.as_posix()}"
ssh_private_key = "{priv.as_posix()}"

[vm_templates.default]

[admin.config]
shell = "zsh"

[defaults]
{extras}
"""
    )
    return cfg


# ---------------------------------------------------------------------------
# _check_secrets
# ---------------------------------------------------------------------------


def test_no_secrets_returns_info(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    assert g.name == "Secrets"
    assert len(g.checks) == 1
    assert g.checks[0].status == Status.INFO
    assert "none" in (g.checks[0].message or "")


def test_secret_resolved_silently_reports_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the operator has AW_SECRET_<NAME> set in env, the would-prompt
    preview reports the secret as available (no prompt needed) and names
    the kind that would provide it."""
    monkeypatch.setenv("AW_SECRET_SHARED", "from-operator-env")
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "Shared API token"

[secret_config]
backends = ["env_var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    msgs = [(c.status, c.name, c.message) for c in g.checks]
    assert any(
        status == Status.OK
        and "shared" in name
        and "available via env_var" in (msg or "")
        for status, name, msg in msgs
    ), msgs


def test_secret_would_prompt_when_no_non_interactive_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no non-interactive backend has a value, the preview reports
    that the secret would prompt at command time (FRD R6 would-prompt
    preview)."""
    monkeypatch.delenv("AW_SECRET_SHARED", raising=False)
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "Shared API token"

[secret_config]
backends = ["env_var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any(
        "shared" in c.name and "would prompt" in (c.message or "")
        for c in warns
    ), [(c.name, c.message) for c in warns]


def test_unused_secret_declaration_warns(tmp_path: Path) -> None:
    """A declared secret with no env entry referencing it gets a WARN."""
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.unused]
description = "nobody references me"

[secret_config]
backends = ["env_var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any("unused" in c.name and "not referenced" in (c.message or "") for c in warns)


def test_soft_skipped_backends_are_info(tmp_path: Path) -> None:
    """When a secret has env_var opted-out (=false), the env_var source's
    would_attempt returns False and doctor reports it as a soft-skip."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "manual" }

[secrets.manual]
description = "force-prompt"
backend_mappings.env_var = false

[secret_config]
backends = ["env_var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    info = [c for c in g.checks if c.status == Status.INFO]
    assert any("manual" in c.name and "env_var" in (c.message or "") for c in info)


def test_mapping_to_active_backend_is_silent(tmp_path: Path) -> None:
    """A backend_mappings entry that points at a backend currently in
    [secret_config].backends produces NO warning."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "shared token"
backend_mappings.env_var = "CUSTOM_NAME"

[secret_config]
backends = ["env_var"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    warns = [c for c in g.checks if c.status == Status.WARN]
    # No warn about backend_mappings (the only WARN here would be about
    # secret not declared, but it IS declared and referenced).
    assert not any("maps env_var" in c.name for c in warns)


def test_mapping_to_undeclared_kind_fails(tmp_path: Path) -> None:
    """A backend_mappings entry referencing a kind that has no
    [secret_backends.<kind>] section AND is not a built-in (env_var /
    prompt) is reported as an error (FRD R6)."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "shared token"
backend_mappings.bogusvault = "x"

[secret_config]
backends = ["env_var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    fails = [c for c in g.checks if c.status == Status.FAIL]
    assert any(
        "maps bogusvault" in c.name and "no [secret_backends.bogusvault]" in (c.message or "")
        for c in fails
    ), [(c.name, c.message) for c in fails]


def test_mapping_to_declared_but_inactive_kind_warns(tmp_path: Path) -> None:
    """A backend_mappings entry referencing a kind that IS declared in
    [secret_backends.*] but NOT listed in [secret_config].backends is
    reported as a warning (mapping has no effect)."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "shared token"
backend_mappings.onepassword = "op://Personal/x/y"

[secret_backends.onepassword]

[secret_config]
backends = ["env_var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any(
        "maps onepassword" in c.name and "not active" in (c.message or "")
        for c in warns
    ), [(c.name, c.message) for c in warns]


def test_builtin_mapping_warns_when_builtin_not_active(tmp_path: Path) -> None:
    """env_var and prompt don't need a [secret_backends.*] section, but a
    backend_mappings.env_var entry is still meaningless if env_var isn't
    listed in [secret_config].backends. The exemption for built-ins must
    not swallow the not-active warning."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "shared token"
backend_mappings.env_var = "CUSTOM_NAME"

[secret_config]
backends = ["prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any(
        "maps env_var" in c.name and "not active" in (c.message or "")
        for c in warns
    ), [(c.name, c.message) for c in warns]


# ---------------------------------------------------------------------------
# _check_env
# ---------------------------------------------------------------------------


def test_check_env_clean_config_reports_ok(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
EDITOR = "nvim"
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_env(config)
    assert g.name == "Env"
    assert any(c.status == Status.OK and "Env keys" in c.name for c in g.checks)


def test_check_env_surfaces_identity_override_warning(tmp_path: Path) -> None:
    """An operator who sets AGENTWORKS_SESSION in their env table triggers
    a config-load warning that doctor re-surfaces in the Env group."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
AGENTWORKS_SESSION = "operator-override"
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_env(config)
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any("AGENTWORKS_SESSION" in (c.message or "") for c in warns)


def test_identity_marker_constant_matches_parse_env_table_phrase(
    tmp_path: Path,
) -> None:
    """``_check_config`` filters identity issues out of the Configuration
    group on the assumption that ``_parse_env_table`` records them with a
    specific marker phrase, and ``_check_env`` re-surfaces them in the
    more specific Env group on the same assumption. If the marker ever
    drifts on one side (e.g. someone changes the wording in either
    ``_parse_env_table`` or ``_IDENTITY_ISSUE_MARKER`` without updating
    the other), the warning either double-reports or vanishes. Pin the
    contract here so that drift surfaces as a test failure."""
    from agentworks.doctor import _IDENTITY_ISSUE_MARKER

    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
AGENTWORKS_SESSION = "operator-override"
""",
    )
    config = load_config(cfg, warn_issues=False)
    identity_issues = [
        issue for issue in config.config_issues
        if _IDENTITY_ISSUE_MARKER in issue
    ]
    assert identity_issues, (
        "the AGENTWORKS_SESSION override in admin.env should produce at "
        "least one config_issue containing the identity marker; the marker "
        "in doctor.py has drifted from the phrase in _parse_env_table"
    )


def test_check_env_reports_cross_scope_conflict(tmp_path: Path) -> None:
    """A key set at both admin and vm scopes is reported as info (the
    operator can run `agw env show` for the effective value)."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
EDITOR = "vim"

[vm_templates.default.env]
EDITOR = "emacs"
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_env(config)
    info = [c for c in g.checks if c.status == Status.INFO]
    assert any("EDITOR" in c.name and "multiple scopes" in (c.message or "") for c in info)


def test_check_env_does_not_flag_two_templates_same_scope_as_conflict(
    tmp_path: Path,
) -> None:
    """Two VM templates that both set EDITOR are mutually exclusive at
    runtime (only one applies per VM), so doctor must NOT report this as
    a multi-scope conflict. Same-scope-kind templates collapse to one
    scope label (FRD R2 scopes: vm/workspace/admin/agent/session)."""
    cfg = _write_config(
        tmp_path,
        extras="""
[vm_templates.default.env]
EDITOR = "vim"

[vm_templates.heavy.env]
EDITOR = "emacs"
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_env(config)
    info = [c for c in g.checks if c.status == Status.INFO]
    assert not any("EDITOR" in c.name and "multiple scopes" in (c.message or "") for c in info), (
        [(c.name, c.message) for c in info]
    )


# ---------------------------------------------------------------------------
# _check_vm_accept_env (ADR 0014: per-VM AcceptEnv-wildcard probe)
# ---------------------------------------------------------------------------


def test_check_vm_accept_env_info_when_no_provisioned_vms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh install (no VMs in DB): the check reports an info, no SSH."""
    from agentworks.db import Database
    from agentworks.doctor import _check_vm_accept_env

    db_path = tmp_path / "test.db"
    monkeypatch.setattr("agentworks.db.DB_PATH", db_path)
    db = Database(db_path)
    db.close()

    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_vm_accept_env(config)

    assert g.name == "VM env support"
    assert any(c.status == Status.INFO and "no provisioned VMs" in (c.message or "") for c in g.checks)


def test_check_vm_accept_env_reports_missing_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe returns non-zero (fragment missing): WARN with reinit hint."""
    from agentworks.db import Database, InitStatus, ProvisioningStatus
    from agentworks.doctor import _check_vm_accept_env

    db_path = tmp_path / "test.db"
    monkeypatch.setattr("agentworks.db.DB_PATH", db_path)
    db = Database(db_path)
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host, "
        "provisioning_status, init_status) VALUES (?, ?, ?, ?, ?, ?)",
        ("vm-legacy", "lima", "agentworks", "100.64.0.5",
         ProvisioningStatus.COMPLETE.value, InitStatus.COMPLETE.value),
    )
    db._conn.commit()
    db.close()

    # Stub the SSH target so the probe runs against a fake transport.
    from types import SimpleNamespace

    fake_target = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=False, returncode=1, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        "agentworks.ssh.admin_exec_target", lambda *a, **k: fake_target
    )

    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_vm_accept_env(config)

    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any(
        "vm-legacy" in c.name and "reinit" in (c.message or "")
        for c in warns
    ), [(c.name, c.message) for c in warns]


def test_check_vm_accept_env_reports_present_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe returns zero (fragment present): OK."""
    from agentworks.db import Database, InitStatus, ProvisioningStatus
    from agentworks.doctor import _check_vm_accept_env

    db_path = tmp_path / "test.db"
    monkeypatch.setattr("agentworks.db.DB_PATH", db_path)
    db = Database(db_path)
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host, "
        "provisioning_status, init_status) VALUES (?, ?, ?, ?, ?, ?)",
        ("vm-modern", "lima", "agentworks", "100.64.0.5",
         ProvisioningStatus.COMPLETE.value, InitStatus.COMPLETE.value),
    )
    db._conn.commit()
    db.close()

    from types import SimpleNamespace

    fake_target = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=True, returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        "agentworks.ssh.admin_exec_target", lambda *a, **k: fake_target
    )

    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_vm_accept_env(config)

    oks = [c for c in g.checks if c.status == Status.OK]
    assert any("vm-modern" in c.name and "AcceptEnv" in (c.message or "") for c in oks)


def test_check_vm_accept_env_skips_unprovisioned_vms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VM with provisioning_status != complete is skipped to avoid
    probing during a still-in-flight provisioning."""
    from agentworks.db import Database, InitStatus, ProvisioningStatus
    from agentworks.doctor import _check_vm_accept_env

    db_path = tmp_path / "test.db"
    monkeypatch.setattr("agentworks.db.DB_PATH", db_path)
    db = Database(db_path)
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host, "
        "provisioning_status, init_status) VALUES (?, ?, ?, ?, ?, ?)",
        ("vm-failed", "lima", "agentworks", None,
         ProvisioningStatus.FAILED.value, InitStatus.FAILED.value),
    )
    db._conn.commit()
    db.close()

    probe_called: list[bool] = []
    from types import SimpleNamespace

    def _track_admin(*a: object, **k: object) -> object:
        probe_called.append(True)
        return SimpleNamespace(run=lambda *a, **k: SimpleNamespace(
            ok=True, returncode=0, stdout="", stderr=""))

    monkeypatch.setattr("agentworks.ssh.admin_exec_target", _track_admin)

    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_vm_accept_env(config)

    assert probe_called == [], "unprovisioned VMs must not be probed"
    assert any(c.status == Status.INFO and "no provisioned VMs" in (c.message or "") for c in g.checks)


def test_check_env_quietly_reports_clean_no_conflicts(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_env(config)
    assert g.name == "Env"
    # No env declared: the helper still records "0 declared, no cross-scope conflicts" OK.
    assert any(c.status == Status.OK and "no cross-scope conflicts" in (c.message or "") for c in g.checks)
