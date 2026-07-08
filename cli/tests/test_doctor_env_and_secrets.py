"""Tests for the env-and-secrets doctor health groups (FRD R6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
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


def test_auto_declared_secrets_are_reported(tmp_path: Path) -> None:
    """Doctor reports EVERY registry secret, auto-declared included --
    they are exactly the ones most likely to prompt at command time,
    so hiding them made doctor unable to predict the next command.
    A bare config still carries the framework-auto-declared
    ``tailscale-auth-key`` (vm-template requirement); it shows with an
    ``(auto)`` marker and an honest would-resolve-via-prompt heads-up.
    """
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config))
    assert g.name == "Secrets"
    statuses = [(c.name, c.status, c.message) for c in g.checks]
    assert statuses == [
        ("Secret 'tailscale-auth-key' (auto)", Status.OK, "would resolve via prompt")
    ], statuses


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
    g = _check_secrets(config, build_registry(config))
    msgs = [(c.status, c.name, c.message) for c in g.checks]
    assert any(
        status == Status.OK
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
    g = _check_secrets(config, build_registry(config))
    oks = [c for c in g.checks if c.status == Status.OK]
    assert any(
        "shared" in c.name and "would resolve via prompt" in (c.message or "")
        for c in oks
    ), [(c.name, c.message) for c in oks]


def test_secret_not_available_when_env_var_unset_and_prompt_opted_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When prompt is opted out via backend_mappings.prompt = false AND
    env-var has no value, doctor reports the secret as WARN (config is
    valid but no backend in the chain would resolve it)."""
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
    g = _check_secrets(config, build_registry(config))
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any(
        "opted-out" in c.name and "not available" in (c.message or "")
        for c in warns
    ), [(c.name, c.message) for c in warns]


def test_mapping_to_undeclared_kind_fails(tmp_path: Path) -> None:
    """A backend_mappings entry referencing a kind that has no
    [secret_backends.<kind>] section AND is not a built-in (env-var /
    prompt) fails the single per-secret row (FRD R6). Exactly one row
    per secret, and FAIL takes precedence over the would-resolve preview
    that env-var/prompt would otherwise emit."""
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
    g = _check_secrets(config, build_registry(config))
    shared_rows = [c for c in g.checks if "shared" in c.name]
    assert len(shared_rows) == 1, shared_rows
    assert shared_rows[0].status == Status.FAIL
    assert shared_rows[0].message == "references unknown backend: bogusvault"


def test_mapping_to_multiple_undeclared_kinds_pluralizes(tmp_path: Path) -> None:
    """When two or more backend_mappings entries reference unknown kinds,
    the single per-secret row lists them sorted and uses the plural
    'backends' in the message."""
    cfg = _write_config(
        tmp_path,
        extras="""
[admin.env]
TOKEN = { secret = "shared" }

[secrets.shared]
description = "shared token"
backend_mappings.zeta-vault = "z"
backend_mappings.alpha-vault = "a"

[secret_config]
backends = ["env-var", "prompt"]
""",
    )
    config = load_config(cfg, warn_issues=False)
    g = _check_secrets(config, build_registry(config))
    shared_rows = [c for c in g.checks if "shared" in c.name]
    assert len(shared_rows) == 1, shared_rows
    assert shared_rows[0].status == Status.FAIL
    assert shared_rows[0].message == "references unknown backends: alpha-vault, zeta-vault"


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
    g, _, _ = _check_config()
    warns = [c for c in g.checks if c.status == Status.WARN]
    assert any("AGENTWORKS_SESSION" in (c.message or "") for c in warns), (
        [(c.name, c.message) for c in warns]
    )


def test_doctor_surfaces_deprecation_nudges(tmp_path: Path, monkeypatch) -> None:
    """Deprecations moved off config_issues onto their own channel (so
    --no-deprecations can silence the ambient per-command warning);
    doctor is the explicit full-health surface and must still show them
    -- the channel split silently dropped them from doctor once.

    Doctor renders the FACT as a tidy one-liner (maintainer ruling,
    2026-07-06): one next step (`agw resource migrate`), no section
    list, no teaching text -- that stays on the ambient warning."""
    cfg = _write_config(tmp_path)  # has [vm_templates.default] + [admin.config]
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, _ = _check_config()
    warns = [
        (c.name, c.message or "")
        for c in g.checks
        if c.status == Status.WARN
    ]
    ((name, message),) = [
        w for w in warns if "deprecated TOML resource" in w[0]
    ]
    # Maintainer-specified row shape: the check NAME carries the fact,
    # the parenthetical carries the one next step.
    assert name == "Config has deprecated TOML resource declarations"
    assert message == "migrate to YAML with `agw resource migrate`"
    # The tidy pin: none of the ambient teaching text leaks into doctor.
    line = f"{name} {message}"
    assert "--no-deprecations" not in line
    assert "resource sample" not in line
    assert "[vm_templates.*]" not in line


def test_doctor_shows_noop_secret_backend_sections(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _write_config(
        tmp_path,
        extras="""
[secret_backends.env-var]
""",
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, _ = _check_config()
    warns = [
        (c.name, c.message or "")
        for c in g.checks
        if c.status == Status.WARN
    ]
    assert any(
        "[secret_backends.env-var]" in name and "remove it" in message
        for name, message in warns
    ), warns


def test_manifest_issues_surface_as_doctor_rows(tmp_path: Path, monkeypatch, capsys) -> None:
    """A typo'd key on a manifest-declared resource (e.g.
    ``github_credentials`` for ``git_credentials`` on an agent-template)
    used to warn ambiently above the report while the Config row said
    ok. Doctor now renders manifest issues as warn rows, and passing
    the pre-loaded set into build_registry keeps the ambient print out
    of doctor's output entirely."""
    from textwrap import dedent

    cfg = _write_config(tmp_path)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "agent.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: agent-template
        metadata:
          name: other
        spec:
          github_credentials: ["github"]
        """)
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, config, registry = _check_config()

    # The suppression half of the fix: doctor passes the pre-loaded set
    # into build_registry, so the ambient "Manifest: ..." print must not
    # appear above the report.
    captured = capsys.readouterr()
    assert "Manifest:" not in captured.out + captured.err

    manifest_rows = [c for c in g.checks if c.name == "Manifest"]
    assert manifest_rows, [c.name for c in g.checks]
    assert manifest_rows[0].status == Status.WARN
    assert "github_credentials" in (manifest_rows[0].message or "")
    assert "agent.yaml" in (manifest_rows[0].message or "")
    # The ok row is withheld when any issue exists.
    assert not any(c.name == "Config is valid" for c in g.checks)
    # The registry still builds (warn, not fail).
    assert registry is not None


def test_clean_manifests_keep_config_valid_row(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, _, registry = _check_config()
    assert any(c.name == "Config is valid" for c in g.checks)
    assert not any(c.name == "Manifest" for c in g.checks)
    assert registry is not None


def test_manifest_load_failure_keeps_other_rows(tmp_path: Path, monkeypatch) -> None:
    """A broken manifest FILE (parse error) gets a fail row without
    short-circuiting the rest of the report: TOML issue rows still
    render, and only the registry-dependent tail is skipped."""
    cfg = _write_config(
        tmp_path,
        extras="""\
[named_console]
bogus_key = 1
""",
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "broken.yaml").write_text("kind: [unclosed\n")
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    g, config, registry = _check_config()

    assert config is not None
    assert registry is None
    fails = [c for c in g.checks if c.name == "Manifest" and c.status == Status.FAIL]
    assert fails and "broken.yaml" in (fails[0].message or "")
    # The TOML unknown-key warn row still rendered after the fail.
    assert any(
        c.name == "Config" and c.status == Status.WARN and "bogus_key" in (c.message or "")
        for c in g.checks
    )
    assert not any(c.name == "Config is valid" for c in g.checks)
