"""Tests for ``agentworks.resources.inspect.describe_resource`` --
the service layer behind ``agw resource describe KIND/NAME``
(Phase 2c).

Stops at framework-uniform fields: kind, name, origin, description,
usage. Kind-specific detail belongs to the per-kind describe commands.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import NotFoundError
from agentworks.resources.inspect import describe_resource


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
        """)
        + dedent(extras),
    )


def _load(cfg_file: Path):
    cfg = load_config(cfg_file, warn_issues=False)
    return build_registry(cfg)


# -- Happy path -------------------------------------------------------------


def test_describes_operator_declared_resource(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.my-key]
        description = "operator-typed note"

        [secret_config]
        backends = ["env-var"]
        """,
    )
    registry = _load(cfg_file)

    desc = describe_resource(registry, "secret", "my-key")

    assert desc.kind == "secret"
    assert desc.name == "my-key"
    assert desc.description == "operator-typed note"
    assert desc.origin is not None
    assert desc.origin.variant == "operator-declared"


def test_describes_template_kind_description(tmp_path: Path) -> None:
    """A formerly template-shaped kind (vm-template) now carries a stored
    description that the generic describe path surfaces."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.dev]
        description = "the dev box"
        cpus = 4
        """,
    )
    registry = _load(cfg_file)

    desc = describe_resource(registry, "vm-template", "dev")

    assert desc.description == "the dev box"


def test_describes_auto_declared_resource_carries_synth_description(
    tmp_path: Path,
) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default]
        apt = ["zsh"]
        """,
    )
    registry = _load(cfg_file)

    desc = describe_resource(registry, "secret", "tailscale-auth-key")

    assert desc.origin is not None
    assert desc.origin.variant == "auto-declared"
    # Phase 2a polish: synth text drives the description column for
    # auto-declared rows.
    assert desc.description.startswith("(auto) ")


@pytest.mark.parametrize(
    "kind",
    [
        "vm-template",
        "agent-template",
        "workspace-template",
        "admin-template",
        "named-console-template",
    ],
)
def test_newly_uniform_kinds_auto_declared_default_gets_synth_description(tmp_path: Path, kind: str) -> None:
    """The five kinds that gained a description field via ``DeclaredResource``
    now get the registry's synthesized text on their auto-declared ``default``
    row (a bare install, no operator config). Before this branch they had no
    description field, so the registry's auto-declared polish skipped them and
    the column showed empty; pin the newly-triggered behavior."""
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    registry = _load(cfg_file)

    desc = describe_resource(registry, kind, "default")

    assert desc.origin is not None
    assert desc.origin.variant == "auto-declared"
    assert desc.description == f"(auto) auto-declared default {kind}"


def test_describe_returns_usage_entries(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default]
        apt = ["zsh"]
        """,
    )
    registry = _load(cfg_file)

    desc = describe_resource(registry, "secret", "tailscale-auth-key")
    assert len(desc.references) >= 1
    # Each entry carries (source, text) -- the renderer formats this
    # as ``<file:line> -- <text>``.
    for entry in desc.references:
        assert isinstance(entry.source, tuple) and len(entry.source) == 2
        assert entry.usage


# -- Error handling ---------------------------------------------------------


def test_unknown_kind_raises_not_found_with_hint(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    registry = _load(cfg_file)

    with pytest.raises(NotFoundError) as excinfo:
        describe_resource(registry, "not_a_real_kind", "x")

    assert excinfo.value.entity_kind == "resource-kind"
    assert excinfo.value.entity_name == "not_a_real_kind"
    # Hint enumerates the known kinds so the operator can recover
    # without separately invoking ``agw resource list``.
    assert "known kinds:" in (excinfo.value.hint or "")


def test_unknown_name_under_known_kind_raises_not_found_with_hint(
    tmp_path: Path,
) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    registry = _load(cfg_file)

    with pytest.raises(NotFoundError) as excinfo:
        describe_resource(registry, "secret", "ghost-secret")

    assert excinfo.value.entity_kind == "secret"
    assert excinfo.value.entity_name == "ghost-secret"
    # Hint points the operator at the cross-kind list scoped to this
    # kind so they can see what *is* available.
    assert "agw resource list --kind secret" in (excinfo.value.hint or "")


# -- CLI surface ------------------------------------------------------------


def test_cli_describe_renders_header_and_usage_sections(tmp_path: Path, monkeypatch) -> None:
    """End-to-end ``agw resource describe KIND/NAME`` emits the
    framework-uniform sections: header (kind/name/description/origin)
    then a Usages list. We don't pin exact whitespace; just confirm
    each section's anchor strings appear in the rendered output.
    """
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default]
        apt = ["zsh"]
        """,
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(app, ["resource", "describe", "secret/tailscale-auth-key"])
    assert result.exit_code == 0, result.stdout
    assert "Resource: secret/tailscale-auth-key" in result.stdout
    assert "Origin:" in result.stdout
    assert "Description:" in result.stdout
    assert "Referenced by:" in result.stdout


def test_cli_describe_unknown_name_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(app, ["resource", "describe", "secret/no-such-secret"])
    assert result.exit_code != 0


def test_cli_describe_unknown_kind_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(app, ["resource", "describe", "no_such_kind/name"])
    assert result.exit_code != 0


def test_cli_describe_rejects_token_without_slash(tmp_path: Path, monkeypatch) -> None:
    """The single-token grammar is KIND/NAME: a bare kind (or a token
    with an empty name half) errors with the example hint before any
    registry work."""
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    for token in ("secret", "secret/"):
        result = CliRunner().invoke(app, ["resource", "describe", token])
        assert result.exit_code != 0
        assert "expected KIND/NAME" in str(result.exception)
