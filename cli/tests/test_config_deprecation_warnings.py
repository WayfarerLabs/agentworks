"""Phase 5 deprecation warnings for TOML resource sections.

Dual-path is permanent policy short of a future major: TOML resource
sections keep loading with today's semantics, but their presence emits
ONE aggregated deprecation issue (aggregated at maintainer direction --
a warning per section was obnoxious on real configs) naming every
present section, the YAML surface, the mover, and the silencer.
Deprecations travel on ``Config.deprecation_issues``, a separate channel
from ``config_issues``, so real issues stay sharp and
``--no-deprecations`` can silence only these.
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


def _deprecations(cfg_path: Path) -> tuple[str, ...]:
    return load_config(cfg_path, warn_issues=False).deprecation_issues


def test_present_sections_aggregate_into_one_warning(tmp_path: Path) -> None:
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

        [admin.config]
        shell = "zsh"
        """,
    )
    (issue,) = _deprecations(cfg)
    # Every present section is named once (grep-able header shapes),
    # in one message -- not one warning per section.
    assert "[secrets.*]" in issue
    assert "[vm_templates.*]" in issue
    assert "[named_console]" in issue
    assert "[admin.config]" in issue
    # And the three pointers: the YAML surface, the mover, the silencer.
    assert "agw resource sample" in issue
    assert "agw resource migrate" in issue
    assert "--no-deprecations" in issue


def test_deprecations_do_not_pollute_config_issues(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        """,
    )
    config = load_config(cfg, warn_issues=False)
    assert config.deprecation_issues
    assert not config.config_issues


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
    assert _deprecations(cfg) == ()


def test_secret_backends_keeps_its_own_no_op_warning(tmp_path: Path) -> None:
    """[secret_backends.*] is not folded into the aggregate: it has the
    dedicated no-op message (pointing at `agw resource migrate --all`),
    on the same suppressible deprecation channel."""
    cfg = _config(
        tmp_path,
        """
        [secret_backends.env-var]
        """,
    )
    issues = _deprecations(cfg)
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
    assert _deprecations(cfg) == ()


def test_cli_no_deprecations_flag_silences_the_warning(
    tmp_path: Path, monkeypatch
) -> None:
    """`agw --no-deprecations <cmd>` suppresses the deprecation warning;
    without the flag it prints. Only deprecations are silenced -- the
    flag does not touch config_issues."""
    from typer.testing import CliRunner

    from agentworks import output
    from agentworks.cli import app

    cfg = _config(
        tmp_path,
        """
        [secrets.npm-token]
        description = "npm token"
        """,
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    # The callback seeds module state per invocation; reset afterwards so
    # test order cannot leak a suppressed state.
    monkeypatch.setattr(output, "_suppress_deprecations", False)

    with_warning = CliRunner().invoke(app, ["resource", "list", "--names-only"])
    assert with_warning.exit_code == 0, with_warning.output
    assert "deprecated TOML resource" in with_warning.output

    silenced = CliRunner().invoke(
        app, ["--no-deprecations", "resource", "list", "--names-only"]
    )
    assert silenced.exit_code == 0, silenced.output
    assert "deprecated" not in silenced.output
