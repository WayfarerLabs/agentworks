"""Command-boundary tests for ``agw secret list`` / ``agw secret describe``
covering the operator-identity soft-load path.

Both commands only ever read declared secrets and their source mappings;
neither reaches the operator's SSH identity, so a missing key file must not
block them (see ``load_config``'s ``workload_gated_issues_fatal`` doc).
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from agentworks.cli import app

if TYPE_CHECKING:
    from pathlib import Path


def _write_config_with_missing_keys(tmp_path: Path) -> Path:
    """A config whose only defect is a nonexistent operator SSH key path
    (the sample config's placeholder, before ``agw config init`` writes a
    real one). No manifests are needed: the framework auto-declares
    ``tailscale-auth-key`` off the always-materialized default
    vm-template, which is enough to exercise both commands."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{(tmp_path / "id.pub").as_posix()}"
        ssh_private_key = "{(tmp_path / "id").as_posix()}"

        [secret_config]
        sources = ["env-var"]
        """)
    )
    assert not (tmp_path / "id.pub").exists()
    assert not (tmp_path / "id").exists()
    return config_path


def test_missing_ssh_keys_do_not_block_secret_list(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config_with_missing_keys(tmp_path)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", config_path)

    result = CliRunner().invoke(app, ["secret", "list"])

    assert result.exit_code == 0, result.output
    assert "tailscale-auth-key" in result.output


def test_missing_ssh_keys_do_not_block_secret_describe(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config_with_missing_keys(tmp_path)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", config_path)

    result = CliRunner().invoke(app, ["secret", "describe", "tailscale-auth-key"])

    assert result.exit_code == 0, result.output
    assert "tailscale-auth-key" in result.output
