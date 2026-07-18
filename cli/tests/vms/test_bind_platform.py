"""``bind_platform``: the imperative composition-root helper's
capability-lifecycle discipline (it still serves the un-migrated
VM-touching commands). Construction is cheap and never resolves;
preflight runs before the operation's single resolve pass (one prompt
session; none at all without declared secrets). The batch variant's
pins (one resolve per batch, shared per-site instance, empty-set
no-op) live with the orchestrated batch composition in
``tests/sessions/test_singular_batch_orchestrated.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.vms import manager as vm_manager
from tests.orchestrated_fixtures import PROXMOX_SECTION, write_operator_config


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """This suite's ``make_config`` delta from the shared fixture:
    nothing baked in (each test names its sites), and deterministic
    platform preflights (lima checks for limactl locally; pretend the
    tool exists regardless of the host)."""
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _make(extra: str = ""):
        return write_operator_config(tmp_path, extra)

    return _make


def _vm(name: str, site: str) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(name=name, site=site)


def test_no_site_secrets_skips_the_resolve_pass(
    make_config, resolve_counter: list[list[str]]
) -> None:
    """A secret-free site's boundary resolve is a no-op: the backend
    loop never runs, so nothing can prompt."""
    config = make_config()
    platform = vm_manager.bind_platform(config, _vm("v1", "lima-local"))  # type: ignore[arg-type]
    assert platform.name == "lima"
    assert resolve_counter == []


def test_secret_bearing_site_resolves_exactly_once(
    make_config, resolve_counter: list[list[str]]
) -> None:
    """The bound platform's declared config secret resolves in the ONE
    boundary pass and ops read it from the resolver's cache."""
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    config = make_config(PROXMOX_SECTION)
    platform = vm_manager.bind_platform(config, _vm("v1", "proxmox"))  # type: ignore[arg-type]
    assert isinstance(platform, ProxmoxPlatform)
    assert platform.resolver is not None
    assert platform.resolver.get("proxmox-token") == "pve-token"
    assert len(resolve_counter) == 1


def test_preflight_failure_prevents_the_resolve_pass(
    make_config, resolve_counter: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifecycle ordering pin: a failing preflight means the
    operator is never asked for a secret (no resolve pass runs)."""
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
    from agentworks.errors import ConnectivityError

    def _boom(self: object, ctx: object) -> None:
        raise ConnectivityError("world broken")

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _boom)
    config = make_config(PROXMOX_SECTION)
    with pytest.raises(ConnectivityError):
        vm_manager.bind_platform(config, _vm("v1", "proxmox"))  # type: ignore[arg-type]
    assert resolve_counter == []


def test_env_targets_join_the_site_secret_pass(
    make_config, resolve_counter: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline one-prompt-session pin: a command's env-chain secret
    (via ``targets=``) and the site's config secret resolve in ONE
    boundary pass; the operation never opens a second session."""
    from agentworks.env import EnvEntry
    from agentworks.secrets import SecretTarget
    from agentworks.secrets.resolver import Resolver

    monkeypatch.setenv("AW_SECRET_API_KEY", "k")
    config = make_config(
        PROXMOX_SECTION + '\n[secrets.api-key]\ndescription = "workload key"\n'
    )
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    resolver = Resolver(config, registry)
    target = SecretTarget(
        vm={"API_KEY": EnvEntry(key="API_KEY", secret="api-key")},
        label="test-shell",
    )
    vm_manager.bind_platform(
        config, _vm("v1", "proxmox"), registry=registry,  # type: ignore[arg-type]
        resolver=resolver, targets=[target],
    )

    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == [
        "api-key",
        "proxmox-token",
    ]
    assert resolver.get("api-key") == "k"
    assert resolver.get("proxmox-token") == "pve-token"
