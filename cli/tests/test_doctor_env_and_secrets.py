"""Tests for the env-and-secrets doctor health groups (FRD R6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.config import load_config
from agentworks.doctor import Status, _check_config, _check_secrets


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


def test_no_secrets_with_default_chain_shows_backends_then_none(
    tmp_path: Path,
) -> None:
    """Default chain present, no secrets declared: an ok row naming the
    backends, and an info row stating no secrets are declared."""
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    assert g.name == "Secrets"
    statuses = [(c.name, c.status, c.message) for c in g.checks]
    assert any(
        name == "Configured backends"
        and status == Status.OK
        and "env-var" in (msg or "")
        and "prompt" in (msg or "")
        for name, status, msg in statuses
    ), statuses
    assert any(
        name == "Declared secrets"
        and status == Status.INFO
        and "none" in (msg or "")
        for name, status, msg in statuses
    ), statuses


def test_empty_backends_no_secrets_warns(tmp_path: Path) -> None:
    """Empty backend chain with no declared secrets: warn (operator may
    have intended to disable secret resolution; nothing to resolve
    anyway, so it's not a hard error)."""
    cfg = _write_config(
        tmp_path,
        extras="""
[secret_config]
backends = []
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any(
        c.name == "Configured backends" and "none active" in (c.message or "")
        for c in warns
    ), [(c.name, c.message) for c in warns]


def test_backends_row_lists_chain_in_precedence_order(tmp_path: Path) -> None:
    """The configured-backends row spells out the chain so operators can
    see the resolution order at a glance, without running secret list."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "..."

[secret_config]
backends = ["prompt", "env-var"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    row = next(c for c in g.checks if c.name == "Configured backends")
    assert row.status == Status.OK
    assert row.message == "prompt, env-var"


def test_secret_resolves_via_env_var_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When AW_SECRET_<NAME> is set, doctor reports the secret as resolving
    via env-var."""
    monkeypatch.setenv("AW_SECRET_SHARED", "from-operator-env")
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "Shared API token"

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    msgs = [(c.status, c.name, c.message) for c in g.checks]
    assert any(
        status == Status.INFO
        and "shared" in name
        and "would resolve via env-var" in (msg or "")
        for status, name, msg in msgs
    ), msgs


def test_secret_resolves_via_prompt_when_env_var_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When env-var has nothing and prompt is in the chain, doctor reports
    the secret as resolving via prompt -- prompt is just another backend."""
    monkeypatch.delenv("AW_SECRET_SHARED", raising=False)
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "Shared API token"

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    infos = [c for c in g.checks if c.status == Status.INFO]
    assert any(
        "shared" in c.name and "would resolve via prompt" in (c.message or "")
        for c in infos
    ), [(c.name, c.message) for c in infos]


def test_secret_not_available_when_env_var_unset_and_prompt_opted_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: when prompt is opted out via backend_mappings.prompt =
    false AND env-var has no value, doctor must report 'not available'.
    Previously preview_resolution short-circuited on prompt without
    checking the opt-out and falsely reported 'would prompt'."""
    monkeypatch.delenv("AW_SECRET_OPTED_OUT", raising=False)
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "opted-out" }

[secrets.opted-out]
description = "Must come from env-var"
backend_mappings.prompt = false

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    fails = [c for c in g.checks if c.status == Status.FAIL]
    assert any(
        "opted-out" in c.name and "not available" in (c.message or "")
        for c in fails
    ), [(c.name, c.message) for c in fails]


def test_unused_secret_declaration_warns(tmp_path: Path) -> None:
    """A declared secret with no env entry referencing it gets a WARN."""
    cfg = _write_config(
        tmp_path,
        extras="""
[secrets.unused]
description = "nobody references me"

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any("unused" in c.name and "not referenced" in (c.message or "") for c in warns)


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
backend_mappings.env-var = "CUSTOM_NAME"

[secret_config]
backends = ["env-var"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    warns = [c for c in g.checks if c.status == Status.WARN]
    # No warn about backend_mappings (the only WARN here would be about
    # secret not declared, but it IS declared and referenced).
    assert not any("maps env-var" in c.name for c in warns)


def test_mapping_to_undeclared_kind_fails(tmp_path: Path) -> None:
    """A backend_mappings entry referencing a kind that has no
    [secret_backends.<kind>] section AND is not a built-in (env-var /
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
backends = ["env-var", "prompt"]
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
backends = ["env-var", "prompt"]
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
    """env-var and prompt don't need a [secret_backends.*] section, but a
    backend_mappings.env-var entry is still meaningless if env-var isn't
    listed in [secret_config].backends. The exemption for built-ins must
    not swallow the not-active warning."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "shared token"
backend_mappings.env-var = "CUSTOM_NAME"

[secret_config]
backends = ["prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config)
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any(
        "maps env-var" in c.name and "not active" in (c.message or "")
        for c in warns
    ), [(c.name, c.message) for c in warns]


# ---------------------------------------------------------------------------
# AGENTWORKS_* identity overrides surface in the Configuration group
# ---------------------------------------------------------------------------


def test_agentworks_identity_override_surfaces_in_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who sets AGENTWORKS_SESSION in their env table triggers
    a config-load warning. Doctor surfaces it once, in the Configuration
    group (there used to be a separate Env group; it was removed as
    redundant since ``agw env show`` is the authoritative inspection
    surface)."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
AGENTWORKS_SESSION = "operator-override"
""",
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _ = _check_config()
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any("AGENTWORKS_SESSION" in (c.message or "") for c in warns), (
        [(c.name, c.message) for c in warns]
    )
