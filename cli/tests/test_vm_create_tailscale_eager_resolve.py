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
from agentworks.secrets.base import SecretDecl
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


# -- --ignore-env (rekey) masking ------------------------------------------


def _env_var_values(*decls: SecretDecl) -> dict[str, str]:
    """What the env-var source resolves for ``decls`` through the real resolution path; ``{}`` when all miss."""
    from agentworks.capabilities.secret_backend import TtyInteractionAccess
    from agentworks.capabilities.secret_backend.env_var import EnvVarBackend, EnvVarSourceConfig
    from agentworks.resources.graph import Readiness
    from agentworks.schema import CapabilityBlock
    from agentworks.secrets import SecretSourceDecl
    from agentworks.secrets.outcomes import ResolutionStatus
    from agentworks.secrets.resolve import ActiveSource, resolve_batch

    source = ActiveSource(
        source=SecretSourceDecl(name="env-var", backend=CapabilityBlock.of("env-var")),
        backend_class=EnvVarBackend,
        config=EnvVarSourceConfig(name="env-var"),
        readiness=Readiness.ready(),
    )
    batch = resolve_batch(list(decls), [source], tty_access=TtyInteractionAccess.DISABLED, interaction_broker=None)
    if all(outcome.status is ResolutionStatus.MISSING for outcome in batch.outcomes):
        return {}
    return batch.complete_or_raise()


@pytest.mark.parametrize("masked", [True, False])
def test_mask_env_var_backend_hides_the_carrier_and_the_mapped_name(
    monkeypatch: pytest.MonkeyPatch, masked: bool
) -> None:
    """``--ignore-env`` makes the env-var source miss both names, a withheld carrier included, then restores them.

    The default secret reads ``AW_SECRET_TAILSCALE_AUTH_KEY``, which the entry
    point has withheld; the mapped one reads the operator's ``CUSTOM_TS_VAR``.
    """
    from agentworks.capabilities.secret_backend import env_var
    from agentworks.vms.manager import _mask_env_var_backend_for

    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "default-stale")
    monkeypatch.setenv("CUSTOM_TS_VAR", "operator-stale")
    env_var.withhold_carriers()
    default = SecretDecl(name="tailscale-auth-key", description="")
    mapped = SecretDecl(name="mapped-key", description="", backend_mappings={"env-var": "CUSTOM_TS_VAR"})
    both = {"tailscale-auth-key": "default-stale", "mapped-key": "operator-stale"}

    with _mask_env_var_backend_for(default, masked=masked), _mask_env_var_backend_for(mapped, masked=masked):
        assert _env_var_values(default, mapped) == ({} if masked else both)
    assert _env_var_values(default, mapped) == both


def test_mask_env_var_backend_no_error_when_var_unset() -> None:
    """When neither the framework name nor the operator override is set
    in the environment, masking is a no-op rather than a KeyError.
    """
    import os

    from agentworks.secrets.base import SecretDecl
    from agentworks.vms.manager import _mask_env_var_backend_for

    # Make sure no relevant var is set; monkeypatch isn't needed since we
    # don't introduce one. (Best-effort hygiene if the test runner inherits
    # a polluted env.)
    os.environ.pop("AW_SECRET_TAILSCALE_AUTH_KEY", None)
    decl = SecretDecl(name="tailscale-auth-key", description="")
    with _mask_env_var_backend_for(decl, masked=True):
        pass  # no exception is the assertion


def test_mask_env_var_backend_restores_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise inside the block (e.g., operator hits Ctrl-C during a
    prompt) must still restore the masked env vars.
    """
    import os

    from agentworks.secrets.base import SecretDecl
    from agentworks.vms.manager import _mask_env_var_backend_for

    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "before-raise")
    decl = SecretDecl(name="tailscale-auth-key", description="")
    with pytest.raises(KeyboardInterrupt), _mask_env_var_backend_for(decl, masked=True):
        assert "AW_SECRET_TAILSCALE_AUTH_KEY" not in os.environ
        raise KeyboardInterrupt
    assert os.environ["AW_SECRET_TAILSCALE_AUTH_KEY"] == "before-raise"
