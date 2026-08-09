"""Tests for the ``secret-backend`` descriptor kind (post-collapse).

One read-only row per registered capability, published by the secrets
code publisher; not manifest-declarable. The ``[secret_config]`` chain
names these rows directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.resources import KIND_REGISTRY, NoUnreferencedDefaultError
from tests.conftest import write_cfg

BUILTIN_BACKENDS = ("env-var", "prompt")


def _write_cfg(path: Path, body: str = "") -> Path:
    """``write_cfg`` under this file's path-taking spelling."""
    return write_cfg(path.parent, settings=body, filename=path.name)


def test_kind_attributes() -> None:
    kind = KIND_REGISTRY["secret-backend"]
    assert kind.miss_policy == "error"
    assert kind.auto_declare_names is None
    assert kind.category == "capability"
    assert kind.description
    # The reserved tier stays (defensive default; plugin-SDD consumer)
    # even though its last reachable member died in the collapse.
    assert kind.builtin_override == "reserved"
    assert "secret-provider" not in KIND_REGISTRY  # collapsed 2026-07-07


def test_descriptor_declares_the_nominal_version_two_backend_contract() -> None:
    from agentworks.capabilities.secret_backend import SecretBackend
    from agentworks.capabilities.secret_backend.kinds import SECRET_BACKEND_DESCRIPTOR as descriptor

    assert descriptor.implementation_contract is SecretBackend
    assert descriptor.contract_version == 2
    assert descriptor.required_operations == {
        "backend_readiness",
        "would_attempt",
        "describe_lookup",
        "create_client",
    }
    assert descriptor.required_attributes == {"interactive", "config_model", "mapping_model"}


def test_core_registry_stores_the_exact_builtin_classes() -> None:
    from agentworks.capabilities.secret_backend import SECRET_BACKEND_REGISTRY, EnvVarBackend, PromptBackend

    assert SECRET_BACKEND_REGISTRY["env-var"] is EnvVarBackend
    assert SECRET_BACKEND_REGISTRY["prompt"] is PromptBackend


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
        assert row.origin.source == "agentworks.capabilities.secret_backend"


def test_no_config_section_can_declare_a_secret_backend(tmp_path: Path) -> None:
    """``secret-backend`` is a capability kind: its rows come from the
    descriptor and from plugins, and config.toml cannot contribute one.

    ``[secret_backends.env-var]`` was the section that looked like it might,
    and it is refused at load now rather than publishing a no-op, so the
    built-in row is the only ``env-var`` there can be.
    """
    with pytest.raises(ConfigError, match="settings only"):
        load_config(
            _write_cfg(
                tmp_path / "config.toml",
                """
                [secret_backends.env-var]
                """,
            ),
            warn_issues=False,
        )
    registry = build_registry(load_config(_write_cfg(tmp_path / "clean.toml", ""), warn_issues=False))
    env_var = registry.lookup("secret-backend", "env-var")
    assert env_var.origin.variant == "built-in"
