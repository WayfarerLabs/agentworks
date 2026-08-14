"""config.toml is settings only (ADR 0022): a resource-declaring section is
a hard error at load, not a deprecation nudge.

These tests pin the flip from the old aggregated warning to
``_raise_for_resource_sections`` (fires with resource sections present at
``resources=True``; no error at ``resources=False`` or for a settings-only
config), and the exempted commands that load ``resources=False`` so they can
still read a config carrying resource sections.

``[secret_backends.*]`` is swept by that same refusal now. It used to be the
one exception, judged by the settings load against the BUILT-IN backend
registry, which warned for a built-in name and refused everything else,
including every plugin backend. Its cases are here rather than with the
secrets tests because what it is now is a retired resource section, and the
only thing that makes it special is its remediation.

The deprecation channel these tests are named for currently carries nothing:
every config.toml deprecation became a hard error. The ``deprecation_issues
== ()`` assertions below are load-is-clean assertions, and they stay honest
if a future nudge is added, because none of these configs would raise it.
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
    assert "settings only" in message
    # The rewrite is the operator's, so the hint carries the whole remedy:
    # the two commands that print the target shape, and the guide section
    # that walks it through.
    hint = excinfo.value.hint or ""
    assert "agw resource sample" in hint
    assert "agw resource describe-kind" in hint
    assert "docs/guides/upgrading-to-0.14.md" in hint


def test_legacy_site_sections_get_the_vm_site_clause(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [azure]
        subscription_id = "0000"
        """,
    )
    with pytest.raises(ConfigError, match=r"\[azure\]/\[proxmox\] sections become vm-site manifests"):
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
        sources = ["env-var", "prompt"]

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


@pytest.mark.parametrize("backend", ["env-var", "onepassword", "envvar"])
def test_secret_backends_is_a_resource_section_whatever_it_names(backend: str, tmp_path: Path) -> None:
    """[secret_backends.*] fails hard and identically for every name.

    It IS a resource-declaring section (it is in ``KIND_SECTIONS``, keyed by
    the ``secret-backend`` kind), so it goes through the same refusal as the
    rest. The three names are the three answers the old split gave: a
    built-in name only warned, and everything else, including a
    correctly-spelled plugin backend that is enabled and healthy
    (``onepassword``) as well as a genuine typo (``envvar``), was refused as
    "an unknown secret backend". The name is not what makes the section
    wrong, so it must not change the answer.
    """
    cfg = _config(tmp_path, f"[secret_backends.{backend}]\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg, warn_issues=False)
    message = str(excinfo.value)
    assert "[secret_backends.*]" in message
    assert "settings only" in message
    # The old refusal's message, which judged the NAME. Nothing should be
    # telling an operator that `onepassword` is an unknown backend.
    assert "unknown secret backend" not in message


def test_secret_backends_is_told_to_delete_and_declare_a_source(tmp_path: Path) -> None:
    """The retired backend row is deleted and replaced by a source declaration.

    ``secret-backend`` remains a capability kind with no declarable form, so
    the replacement manifest is a ``secret-source`` that selects it. The
    chain then names that declared source.
    """
    cfg = _config(tmp_path, "[secret_backends.env-var]\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg, warn_issues=False)
    message = str(excinfo.value)
    assert "carries no configuration, so delete it" in message
    assert "Rewrite" not in message
    hint = excinfo.value.hint or ""
    assert "Use the implied env-var and prompt source names as-is" in hint
    assert "declare a secret-source manifest" in hint
    assert "list the source name in [secret_config].sources" in hint


def test_mixed_sections_get_both_remediations_in_one_read(tmp_path: Path) -> None:
    """A config carrying both kinds of section names all of them, but tells
    the operator to REWRITE only the ones that become manifests. Lumping
    ``[secret_backends.*]`` into the rewrite clause would send them looking
    for a manifest shape for a section that has none."""
    cfg = _config(
        tmp_path,
        """
        [secret_backends.env-var]

        [secrets.npm-token]
        description = "npm token"
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg, warn_issues=False)
    message = str(excinfo.value)
    assert "[secrets.*]" in message
    assert "[secret_backends.*]" in message
    # The rewrite clause names the rewritable section and only that one.
    assert "Rewrite [secrets.*] as YAML manifests" in message
    assert "carries no configuration, so delete it" in message


def test_secret_backends_is_behind_the_resources_false_escape_hatch(tmp_path: Path) -> None:
    """The point of moving the check: the commands that ARE the remediation
    can now read a config carrying ``[secret_backends.*]``.

    It used to be refused by the SETTINGS load, which no escape hatch covers,
    so a section carrying no configuration took down ``resource sample
    --write`` and ``resource schema --write``, the two commands an operator
    needs to do the rewrite at all.
    """
    cfg = _config(tmp_path, "[secret_backends.onepassword]\n")
    config = load_config(cfg, warn_issues=False, resources=False)
    assert config.operator is not None


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
