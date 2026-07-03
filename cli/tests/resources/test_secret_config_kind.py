"""Tests for the ``secret-config`` kind: the singleton chain row.

``[secret_config]`` stays TOML-only config, but it names resources, so
``Config.publish_to`` publishes it and the chain becomes reference
edges validated at finalize. The runtime (``resolver_for``) reads the
chain from the registry row, never from Config.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.resources import Registry


def _config(tmp_path: Path, extras: str = "") -> object:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(extras)
    )
    return load_config(cfg, warn_issues=False)


def test_config_publishes_secret_config_row(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        [secret_config]
        backends = ["prompt"]
        """,
    )
    registry = build_registry(config)
    row = registry.lookup("secret-config", "default")
    assert row.backends == ("prompt",)
    assert row.origin is not None
    assert row.origin.variant == "operator-declared"


def test_default_chain_publishes_and_references_builtins(tmp_path: Path) -> None:
    """No [secret_config] table: the loader-defaulted chain publishes
    and its edges land as usage on the built-in backend rows."""
    config = _config(tmp_path)
    registry = build_registry(config)
    row = registry.lookup("secret-config", "default")
    assert row.backends == ("env-var", "prompt")
    env_var_row = registry.lookup("secret-backend", "env-var")
    assert any(
        ref.source == ("secret-config", "default")
        for ref in env_var_row.references
    )


def test_bare_registry_seeds_empty_chain_sentinel() -> None:
    """A registry finalized without a config publisher gets the
    always-materialize sentinel: empty chain, no edges, no semantic
    validation -- hand-built test registries finalize cleanly."""
    registry = Registry.empty()
    registry.finalize()
    row = registry.lookup("secret-config", "default")
    assert row.backends == ()
    assert row.origin is not None
    assert row.origin.variant == "auto-declared"
    assert row.origin.source == ("framework", "always-materialize")
