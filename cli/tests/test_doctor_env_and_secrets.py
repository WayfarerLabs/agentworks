"""Tests for the env-and-secrets doctor health groups (FRD R6)."""

from __future__ import annotations

from pathlib import Path

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


def test_secret_with_active_backend_reports_first_attempting(tmp_path: Path) -> None:
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
        status == Status.OK and "shared" in name and "env_var" in (msg or "")
        for status, name, msg in msgs
    ), msgs


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


def test_check_env_quietly_reports_clean_no_conflicts(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_env(config)
    assert g.name == "Env"
    # No env declared: the helper still records "0 declared, no cross-scope conflicts" OK.
    assert any(c.status == Status.OK and "no cross-scope conflicts" in (c.message or "") for c in g.checks)
