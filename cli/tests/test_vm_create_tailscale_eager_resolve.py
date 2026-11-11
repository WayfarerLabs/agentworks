"""Tests for the vm-create Tailscale-resolution path.

The Tailscale secret resolves through the operation's resolver at the
preflight boundary, BEFORE any state-mutating provisioning starts (the
vm-template node declares it, the walk union registers it, and the
node's preflight predicts it centrally); the install runner receives
the value as a keyword argument; no ``env=`` injection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.secrets.policy import TtyInteractionPolicy
from tests.conftest import ManifestDoc, write_cfg

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _resolve_tailscale_key(config, registry, vm_tmpl) -> str:  # type: ignore[no-untyped-def]
    """The create-path shape: the vm-template node declares the key
    (its ``secret_refs``, which the walk union registers, and its
    ``config_secret_refs``, which the preflight sweep predicts over),
    the sweep runs, the boundary resolve runs, ops read from the
    cache."""
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import vm_template_node

    resolver = Resolver(config, registry, interaction=TtyInteractionPolicy.REFUSE)
    node = vm_template_node(vm_tmpl)
    for name in node.secret_refs():
        resolver.register_name(name)
    preflight_all([node], RunContext(config=config), registry=registry, interaction=TtyInteractionPolicy.REFUSE)
    resolver.resolve()
    return resolver.get(vm_tmpl.tailscale_auth_key)


def _write_cfg(
    tmp_path: Path,
    *,
    settings: str = "",
    manifests: Sequence[ManifestDoc | str] = (),
) -> Path:
    """``write_cfg`` under this file's keyword spelling."""
    return write_cfg(tmp_path, *manifests, settings=settings, filename="c.toml")


def test_boundary_resolves_tailscale_from_env_var(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The framework's env-var backend picks up ``AW_SECRET_TAILSCALE_AUTH_KEY``
    (default mapping for the ``tailscale-auth-key`` secret). The legacy
    ``AW_TAILSCALE_AUTH_KEY`` name is gone -- the framework's default
    backend convention is ``AW_SECRET_<UPPER_NAME>``.
    """
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
    )
    config = load_config(cfg, warn_issues=False)

    # The framework's env-var convention: AW_SECRET_<NAME>, dashes -> _.
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-from-env")

    # Build a resolved template instance directly (no DB scaffolding).
    from agentworks.bootstrap import build_registry
    from agentworks.vms.templates import resolve_template

    registry = build_registry(config)
    vm_tmpl = resolve_template(registry, "default")
    assert _resolve_tailscale_key(config, registry, vm_tmpl) == "tskey-from-env"


def test_boundary_uses_custom_tailscale_secret_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VMTemplate that overrides ``tailscale_auth_key = "custom-ts"``
    resolves the ``custom-ts`` secret name, not the default
    ``tailscale-auth-key``.
    """
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("vm-template", "azure-prod", {"tailscale_auth_key": "custom-ts"})],
    )
    config = load_config(cfg, warn_issues=False)

    monkeypatch.setenv("AW_SECRET_CUSTOM_TS", "tskey-custom")

    from agentworks.bootstrap import build_registry
    from agentworks.vms.templates import resolve_template

    registry = build_registry(config)
    vm_tmpl = resolve_template(registry, "azure-prod")
    assert _resolve_tailscale_key(config, registry, vm_tmpl) == "tskey-custom"


def test_template_preflight_detects_an_ordinary_env_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-impact preview reads and discards the value, so absence is definitive."""
    cfg = _write_cfg(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.delenv("AW_SECRET_TAILSCALE_AUTH_KEY", raising=False)

    from agentworks.bootstrap import build_registry
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.vms.nodes import vm_template_node
    from agentworks.vms.templates import resolve_template

    registry = build_registry(config)
    vm_tmpl = resolve_template(registry, "default")
    with pytest.raises(ConfigError):
        preflight_all(
            [vm_template_node(vm_tmpl)],
            RunContext(config=config),
            registry=registry,
            interaction=TtyInteractionPolicy.REFUSE,
        )
