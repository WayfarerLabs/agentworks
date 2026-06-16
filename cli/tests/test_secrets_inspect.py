"""Tests for ``agentworks.secrets.inspect`` -- the table builder behind
``agw secret list``."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agentworks.config import load_config
from agentworks.secrets.inspect import build_secret_table


def _write_base(config_path: Path, *, extras: str = "") -> None:
    pub = config_path.parent / "id.pub"
    priv = config_path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [vm_templates.default]
        apt = ["zsh"]
        """)
        + dedent(extras),
    )


def test_no_secrets_returns_empty_rows_but_active_backends(tmp_path: Path) -> None:
    """No declared secrets: backend chain is still present (default
    env-var + prompt) but rows is empty. CLI renders the empty-state
    message off of this shape."""
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    cfg = load_config(cfg_file, warn_issues=False)

    table = build_secret_table(cfg)
    assert table.backend_kinds == ("env-var", "prompt")
    assert table.rows == ()


def test_rows_sorted_alphabetically_by_secret_name(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        Z = { secret = "z-token" }
        A = { secret = "a-token" }
        M = { secret = "m-token" }

        [secrets.z-token]
        description = "Z"

        [secrets.a-token]
        description = "A"

        [secrets.m-token]
        description = "M"
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    table = build_secret_table(cfg)
    assert [r.name for r in table.rows] == ["a-token", "m-token", "z-token"]


def test_env_var_cell_shows_default_convention_identifier(tmp_path: Path) -> None:
    """No explicit mapping: env-var cell shows ``AW_SECRET_<UPPER>``."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        TOKEN = { secret = "github-token" }

        [secrets.github-token]
        description = "GitHub PAT"
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    table = build_secret_table(cfg)
    row = table.rows[0]
    env_var_cell = next(c for c in row.cells if c.backend_kind == "env-var")
    assert env_var_cell.would_attempt is True
    assert env_var_cell.identifier == "AW_SECRET_GITHUB_TOKEN"


def test_env_var_cell_shows_mapping_override(tmp_path: Path) -> None:
    """``backend_mappings.env-var = "..."`` is the identifier shown in
    the cell, not the default convention."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        TOKEN = { secret = "github-token" }

        [secrets.github-token]
        description = "GitHub PAT"
        backend_mappings.env-var = "GITHUB_TOKEN"
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    table = build_secret_table(cfg)
    env_var_cell = next(c for c in table.rows[0].cells if c.backend_kind == "env-var")
    assert env_var_cell.identifier == "GITHUB_TOKEN"


def test_env_var_cell_when_opted_out_reports_disabled(tmp_path: Path) -> None:
    """``backend_mappings.env-var = false``: would_attempt is False so the
    renderer reports ``disabled``. Identifier is None."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        TOKEN = { secret = "force-prompt" }

        [secrets.force-prompt]
        description = "Always prompt"
        backend_mappings.env-var = false
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    table = build_secret_table(cfg)
    env_var_cell = next(c for c in table.rows[0].cells if c.backend_kind == "env-var")
    assert env_var_cell.would_attempt is False
    assert env_var_cell.identifier is None


def test_prompt_cell_has_no_static_identifier(tmp_path: Path) -> None:
    """Prompt always attempts but has no static lookup key; CLI renders
    this as ``enabled``."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        TOKEN = { secret = "any" }

        [secrets.any]
        description = "any"
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    table = build_secret_table(cfg)
    prompt_cell = next(c for c in table.rows[0].cells if c.backend_kind == "prompt")
    assert prompt_cell.would_attempt is True
    assert prompt_cell.identifier is None


def test_column_order_matches_backend_chain_precedence(tmp_path: Path) -> None:
    """The columns appear in [secret_config].backends order so operators
    see the resolution order directly in the table layout."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        TOKEN = { secret = "x" }

        [secrets.x]
        description = "x"

        [secret_config]
        backends = ["prompt", "env-var"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    table = build_secret_table(cfg)
    assert table.backend_kinds == ("prompt", "env-var")


def test_empty_backend_chain_yields_no_columns(tmp_path: Path) -> None:
    """``backends = []`` opts out of all resolution; the table has no
    backend columns. (An operator who declares secrets in this state
    would have already hit the unreachable-secret config error at load,
    so this test uses no-declared-secrets to exercise the empty-chain
    shape in isolation.)"""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secret_config]
        backends = []
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    table = build_secret_table(cfg)
    assert table.backend_kinds == ()
    assert table.rows == ()
