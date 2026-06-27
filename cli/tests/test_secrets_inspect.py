"""Tests for ``agentworks.secrets.inspect`` -- the table builder behind
``agw secret list``."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.secrets.inspect import build_secret_table


def _build_table(cfg_file: Path):
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)
    return build_secret_table(cfg, registry)


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


def test_no_operator_secrets_still_shows_auto_declared(tmp_path: Path) -> None:
    """No operator-declared secrets, but Phase 1c's VMTemplate
    ``tailscale_auth_key`` requirement always auto-declares the
    ``tailscale-auth-key`` secret. The table iterates the Registry
    (per Phase 1e) so that auto-declared row is surfaced.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)

    table = _build_table(cfg_file)
    assert table.backend_kinds == ("env-var", "prompt")
    names = [r.name for r in table.rows]
    assert "tailscale-auth-key" in names
    # The auto-declared row carries its origin breadcrumb.
    ts = next(r for r in table.rows if r.name == "tailscale-auth-key")
    assert ts.origin_text == "auto-declared by vm_template:default"
    # Counts match the operator/auto split.
    assert table.operator_count == 0
    assert table.auto_count >= 1


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
    table = _build_table(cfg_file)
    # Operator-declared secrets are sorted alphabetically; the
    # registry also auto-declares ``tailscale-auth-key`` via Phase 1c's
    # VMTemplate requirement, so filter to only the operator-typed
    # names for the order assertion.
    operator_typed = {"a-token", "m-token", "z-token"}
    seen = [r.name for r in table.rows if r.name in operator_typed]
    assert seen == ["a-token", "m-token", "z-token"]


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
    table = _build_table(cfg_file)
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
    table = _build_table(cfg_file)
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
    table = _build_table(cfg_file)
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
    table = _build_table(cfg_file)
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
    table = _build_table(cfg_file)
    assert table.backend_kinds == ("prompt", "env-var")


def test_empty_backend_chain_yields_no_columns(tmp_path: Path) -> None:
    """``backends = []`` opts out of all resolution; the table has no
    backend columns. Operator-declared secrets in this state would
    trip the unreachable-secret config-load error. The
    auto-declared ``tailscale-auth-key`` row (Phase 1c) is still
    surfaced in the table since the env-and-secrets reachability check
    only inspects operator-declared secrets.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secret_config]
        backends = []
        """,
    )
    table = _build_table(cfg_file)
    assert table.backend_kinds == ()
    # Auto-declared rows still appear (each with empty cells, since
    # there are no backend columns).
    assert all(r.cells == () for r in table.rows)
    assert table.operator_count == 0
