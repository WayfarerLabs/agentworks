"""Phase 5 per-section deprecation warnings for TOML resource sections.

Dual-path is permanent policy short of a future major: TOML resource
sections keep loading with today's semantics, but each present section
emits one deprecation issue naming the section and pointing at the
YAML surface (`agw resource sample`) and the mover
(`agw resource migrate`).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agentworks.config import load_config


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


def _deprecation_issues(cfg_path: Path) -> list[str]:
    config = load_config(cfg_path, warn_issues=False)
    return [i for i in config.config_issues if "deprecated" in i]


def test_each_present_resource_section_warns_once(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"

        [secrets.other]
        description = "other"

        [vm_templates.default]
        cpus = 4

        [named_console]
        tmux_layout = "tiled"
        """,
    )
    issues = _deprecation_issues(cfg)
    # One warning per section KIND present, not per resource.
    assert len([i for i in issues if i.startswith("[secrets.*]")]) == 1
    assert len([i for i in issues if i.startswith("[vm_templates.*]")]) == 1
    assert len([i for i in issues if i.startswith("[named_console]")]) == 1


def test_warning_names_the_commands(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        """,
    )
    (issue,) = _deprecation_issues(cfg)
    assert issue.startswith("[secrets.*]")
    assert "agw resource migrate secret" in issue
    assert "agw resource sample secret" in issue


def test_config_only_toml_warns_nothing(tmp_path: Path) -> None:
    """Settings sections ([operator], [secret_config], ...) are config,
    not resources: a fully-migrated config loads without a single
    deprecation issue."""
    cfg = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["env-var", "prompt"]

        [defaults]
        """,
    )
    assert _deprecation_issues(cfg) == []


def test_secret_backends_keeps_its_own_no_op_warning(tmp_path: Path) -> None:
    """[secret_backends.*] is not double-warned: it has the dedicated
    no-op message (now pointing at `agw resource migrate --all`), not
    the generic resource-section deprecation."""
    cfg = _config(
        tmp_path,
        """
        [secret_backends.env-var]
        """,
    )
    issues = _deprecation_issues(cfg)
    assert len(issues) == 1
    assert issues[0].startswith("[secret_backends.env-var]")
    assert "agw resource migrate --all" in issues[0]


def test_shipped_sample_config_warns_nothing(tmp_path: Path) -> None:
    """The shipped sample is YAML-first: as-shipped (resource examples
    commented out) it produces zero deprecation issues."""
    sample = (
        Path(__file__).resolve().parent.parent
        / "agentworks"
        / "sample-config.toml"
    )
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    text = sample.read_text()
    text = text.replace('ssh_public_key = "~/.ssh/id_ed25519.pub"', f'ssh_public_key = "{pub.as_posix()}"')
    text = text.replace('ssh_private_key = "~/.ssh/id_ed25519"', f'ssh_private_key = "{priv.as_posix()}"')
    cfg = tmp_path / "config.toml"
    cfg.write_text(text)
    assert _deprecation_issues(cfg) == []
