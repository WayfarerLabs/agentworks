"""The VM composition roots' capability-lifecycle discipline
(formerly pinned against the retired imperative ``bind_platform``
helper; assertions preserved, driven through the orchestrated roots).
Construction is cheap and never resolves; preflight runs before the
operation's single resolve pass (one prompt session; none at all
without declared secrets); a command's env-chain targets join the
site secrets in that ONE pass. The batch variant's pins (one resolve
per batch, shared per-site instance, empty-set no-op) live with the
orchestrated batch composition in
``tests/sessions/test_singular_batch_orchestrated.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.vms import manager as vm_manager
from tests.orchestrated_fixtures import PROXMOX_SECTION, write_operator_config

if TYPE_CHECKING:
    from agentworks.db import Database, VMRow


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


def _seed_vm(db: Database, site: str) -> VMRow:
    db.insert_vm("v1", site=site, hostname="v1")
    db.update_vm_tailscale("v1", "100.64.0.9")
    vm = db.get_vm("v1")
    assert vm is not None
    return vm


def test_no_site_secrets_skips_the_resolve_pass(
    db: Database, make_config, resolve_counter: list[list[str]]  # noqa: ANN001
) -> None:
    """A secret-free site's boundary resolve is a no-op: the backend
    loop never runs, so nothing can prompt."""
    config = make_config()
    vm_node = vm_manager._live_vm_boundary(db, config, _seed_vm(db, "lima-local"))
    assert vm_node.site.platform.name == "lima"
    assert resolve_counter == []


def test_secret_bearing_site_resolves_exactly_once(
    db: Database, make_config, resolve_counter: list[list[str]]  # noqa: ANN001
) -> None:
    """The bound platform's declared config secret resolves in the ONE
    boundary pass and ops read it from the resolver's cache."""
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    config = make_config(PROXMOX_SECTION)
    platform = vm_manager._live_vm_boundary(
        db, config, _seed_vm(db, "proxmox")
    ).site.platform
    assert isinstance(platform, ProxmoxPlatform)
    assert platform.resolver is not None
    assert platform.resolver.get("proxmox-token") == "pve-token"
    assert len(resolve_counter) == 1


def test_preflight_failure_prevents_the_resolve_pass(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
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
        vm_manager._live_vm_boundary(db, config, _seed_vm(db, "proxmox"))
    assert resolve_counter == []


def test_env_targets_join_the_site_secret_pass(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline one-prompt-session pin: a command's env-chain secret
    (via ``targets=``) and the site's config secret resolve in ONE
    boundary pass; the operation never opens a second session."""
    from agentworks.bootstrap import build_registry
    from agentworks.env import EnvEntry
    from agentworks.secrets import SecretTarget

    monkeypatch.setenv("AW_SECRET_API_KEY", "k")
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    config = make_config(
        PROXMOX_SECTION + '\n[secrets.api-key]\ndescription = "workload key"\n'
    )
    registry = build_registry(config)
    target = SecretTarget(
        vm={"API_KEY": EnvEntry(key="API_KEY", secret="api-key")},
        label="test-shell",
    )
    with vm_manager.gated_vm_boundary(
        db, config, registry, _seed_vm(db, "proxmox"), targets=[target]
    ) as (_vm_node, resolver):
        pass

    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == [
        "api-key",
        "proxmox-token",
    ]
    assert resolver.get("api-key") == "k"
    assert resolver.get("proxmox-token") == "pve-token"
