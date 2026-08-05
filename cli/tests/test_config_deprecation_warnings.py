"""config.toml is settings only (ADR 0022): a resource-declaring section is
a hard error at load, not a deprecation nudge.

These tests pin the flip from the old aggregated warning to
``_raise_for_resource_sections`` (fires with resource sections present at
``resources=True``; no error at ``resources=False`` or for a settings-only
config), the remaining deprecation-channel content (the ``[secret_backends]``
no-op), and the exempted commands that
load ``resources=False`` so they can still read a config carrying resource
sections.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import load_config
from agentworks.errors import ConfigError


def _config(tmp_path: Path, extras: str = "") -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
        + dedent(extras)
    )
    return cfg


def test_resource_sections_hard_error_naming_sections(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"

        [vm_templates.default]
        cpus = 4

        [named_console]
        tmux_layout = "tiled"

        [admin.config]
        shell = "zsh"
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg, warn_issues=False)
    message = str(excinfo.value)
    # Every present section is named (grep-able header shapes).
    assert "[secrets.*]" in message
    assert "[vm_templates.*]" in message
    assert "[named_console]" in message
    assert "[admin.config]" in message
    # And it points at the two remediation commands and says settings-only.
    assert "agw resource migrate" in message
    assert "agw resource sample" in message
    assert "settings only" in message


def test_legacy_site_sections_get_the_vm_site_clause(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [azure]
        subscription_id = "0000"
        """,
    )
    with pytest.raises(ConfigError, match="migrate as vm-site"):
        load_config(cfg, warn_issues=False)


def test_no_vm_site_clause_without_a_legacy_site(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg, warn_issues=False)
    assert "vm-site" not in str(excinfo.value)


def test_resources_false_skips_the_check(tmp_path: Path) -> None:
    """The escape hatch: ``resources=False`` loads a config carrying
    resource sections (settings load identically), and collects no
    resource-section deprecation."""
    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        """,
    )
    config = load_config(cfg, warn_issues=False, resources=False)
    assert config.operator is not None
    assert config.deprecation_issues == ()


def test_settings_only_config_loads_without_error(tmp_path: Path) -> None:
    """A fully-migrated config (only settings sections) loads normally with
    no error and no deprecation."""
    cfg = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["env-var", "prompt"]

        [defaults]
        """,
    )
    config = load_config(cfg, warn_issues=False)
    assert config.deprecation_issues == ()


def test_shipped_sample_config_loads_clean(tmp_path: Path) -> None:
    """The shipped sample is YAML-first: as-shipped (resource examples
    commented out) it loads with no error and no deprecation."""
    sample = Path(__file__).resolve().parent.parent / "agentworks" / "sample-config.toml"
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    text = sample.read_text()
    text = text.replace('ssh_public_key = "~/.ssh/id_ed25519.pub"', f'ssh_public_key = "{pub.as_posix()}"')
    text = text.replace('ssh_private_key = "~/.ssh/id_ed25519"', f'ssh_private_key = "{priv.as_posix()}"')
    cfg = tmp_path / "config.toml"
    cfg.write_text(text)
    config = load_config(cfg, warn_issues=False)
    assert config.deprecation_issues == ()


def test_secret_backends_keeps_its_no_op_deprecation(tmp_path: Path) -> None:
    """[secret_backends.*] is NOT a resource section (it is a capability-kind
    no-op), so it stays a deprecation on the channel rather than a hard
    error, pointing at `agw resource migrate --all`."""
    cfg = _config(
        tmp_path,
        """
        [secret_backends.env-var]
        """,
    )
    config = load_config(cfg, warn_issues=False)
    assert len(config.deprecation_issues) == 1
    assert config.deprecation_issues[0].startswith("[secret_backends.env-var]")
    assert "agw resource migrate --all" in config.deprecation_issues[0]
    assert config.noop_secret_backend_sections == ("[secret_backends.env-var]",)


def test_defaults_platform_is_a_hard_error(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [defaults]
        platform = "azure"
        """,
    )
    with pytest.raises(ConfigError, match=r"unexpected keys in \[defaults\]: platform"):
        load_config(cfg, warn_issues=False)


# ---------------------------------------------------------------------------
# Exempted commands: they load resources=False and so still run against a
# config that carries resource sections; a normal command errors.
# ---------------------------------------------------------------------------


def test_normal_command_errors_against_a_resource_declaring_config(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        """,
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    result = CliRunner().invoke(app, ["resource", "list", "--names-only"])
    assert result.exit_code != 0
    # CliRunner invokes the Typer app directly (not the ``main()`` wrapper that
    # renders AgentworksError to stderr), so the hard error surfaces as the
    # captured exception.
    assert isinstance(result.exception, ConfigError)
    assert "settings only" in str(result.exception)


def test_resource_migrate_runs_against_a_resource_declaring_config(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        """,
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    dry = CliRunner().invoke(app, ["resource", "migrate", "secret", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "secret/npm-token" in dry.output


def test_resource_sample_write_runs_against_a_resource_declaring_config(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        """,
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    written = CliRunner().invoke(app, ["resource", "sample", "secret", "--write", "samples.yaml"])
    assert written.exit_code == 0, written.output
    assert (tmp_path / "resources" / "samples.yaml").exists()


def test_resource_edit_fallback_runs_against_a_resource_declaring_config(tmp_path: Path, monkeypatch) -> None:
    """`resource edit` falls back to a settings-only manifest scan when the
    registry build is unavailable; it must not hit the hard error. With no
    matching manifest it reports not-found, NOT the settings-only
    ConfigError."""
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        """,
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("EDITOR", "true")
    result = CliRunner().invoke(app, ["resource", "edit", "secret/does-not-exist"])
    # It reached the manifest scan (settings-only), not the resource-section
    # hard error: the output must not carry the settings-only message.
    assert "settings only" not in result.output
