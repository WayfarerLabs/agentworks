"""Tests for the ``secret-backend`` descriptor kind (post-collapse).

One read-only row per registered capability, published by the secrets
code publisher; not manifest-declarable. The ``[secret_config]`` chain
names these rows directly.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.resources import KIND_REGISTRY, NoUnreferencedDefaultError

BUILTIN_BACKENDS = ("env-var", "prompt")


def _write_cfg(path: Path, body: str = "") -> Path:
    pub = path.parent / "id.pub"
    priv = path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(body),
    )
    return path


def test_kind_attributes() -> None:
    kind = KIND_REGISTRY["secret-backend"]
    assert kind.kind == "secret-backend"
    assert kind.miss_policy == "error"
    assert kind.auto_declare_names is None
    assert kind.manifest_declarable is False
    # The reserved tier stays (defensive default; plugin-SDD consumer)
    # even though its last reachable member died in the collapse.
    assert kind.builtin_override == "reserved"
    assert "secret-provider" not in KIND_REGISTRY  # collapsed 2026-07-07


def test_synthesize_raises() -> None:
    kind = KIND_REGISTRY["secret-backend"]
    with pytest.raises(NoUnreferencedDefaultError):
        kind.synthesize(())


def test_capability_descriptors_published(tmp_path: Path) -> None:
    """One descriptor row per registered capability, from the secrets
    code publisher (the bundled backend manifests died in the Phase 5.5
    collapse)."""
    cfg = load_config(_write_cfg(tmp_path / "config.toml"), warn_issues=False)
    registry = build_registry(cfg)
    for backend_name in BUILTIN_BACKENDS:
        row = registry.lookup("secret-backend", backend_name)
        assert row.name == backend_name
        assert row.origin.variant == "built-in"
        assert row.origin.source == "agentworks.secrets"


def test_legacy_toml_backend_section_does_not_override_built_in(tmp_path: Path) -> None:
    """``[secret_backends.env-var]`` is a deprecated no-op: it publishes
    nothing (it warns at load), and the descriptor row survives
    untouched."""
    cfg = load_config(
        _write_cfg(
            tmp_path / "config.toml",
            """
            [secret_backends.env-var]
            """,
        ),
        warn_issues=False,
    )
    registry = build_registry(cfg)
    env_var = registry.lookup("secret-backend", "env-var")
    assert env_var.origin.variant == "built-in"
