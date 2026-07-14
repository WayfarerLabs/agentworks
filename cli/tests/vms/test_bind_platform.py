"""``bind_platform`` / ``bind_platforms``: the composition-root helper's
capability-lifecycle discipline. Construction is cheap and never
resolves; preflight runs before the operation's single resolve pass
(one prompt session; none at all without declared secrets); a batch
shares one resolver across sites; the registry build stays lazy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.config import load_config
from agentworks.vms import manager as vm_manager

PROXMOX_SECTION = """
[proxmox]
api_url = "https://pve:8006"
node = "pve1"
token_id = "agw@pam!agw"
template_vmid = 9000
"""


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    # Deterministic platform preflights: lima checks for limactl
    # locally; pretend the tool exists regardless of the host.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _make(extra: str = ""):
        path = tmp_path / "config.toml"
        path.write_text(
            f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
            + extra
        )
        return load_config(path, warn_issues=False, warn_deprecations=False)

    return _make


@pytest.fixture
def resolve_counter(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Count boundary resolve passes through the real backend loop."""
    from agentworks.secrets import resolve as secrets_resolve

    calls: list[object] = []
    real = secrets_resolve.resolve_secrets

    def _counting(secrets: list[object], *args: object, **kwargs: object) -> dict[str, str]:
        calls.append([getattr(s, "name", s) for s in secrets])
        return real(secrets, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets_resolve, "resolve_secrets", _counting)
    return calls


def _vm(name: str, site: str) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(name=name, site=site)


def test_no_site_secrets_skips_the_resolve_pass(
    make_config, resolve_counter: list[object]
) -> None:
    """A secret-free site's boundary resolve is a no-op: the backend
    loop never runs, so nothing can prompt."""
    config = make_config()
    platform = vm_manager.bind_platform(config, _vm("v1", "lima-local"))  # type: ignore[arg-type]
    assert platform.name == "lima"
    assert resolve_counter == []


def test_secret_bearing_site_resolves_exactly_once(
    make_config, resolve_counter: list[object]
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
    make_config, resolve_counter: list[object], monkeypatch: pytest.MonkeyPatch
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


def test_bind_platforms_one_resolve_and_one_instance_per_site(
    make_config, resolve_counter: list[object]
) -> None:
    """Two VMs at the same secret-bearing site share one bound platform
    and the whole batch shares ONE resolve pass (prompt-once across a
    batch command, not just within one site)."""
    config = make_config(PROXMOX_SECTION)
    vms = [_vm("v1", "proxmox"), _vm("v2", "proxmox"), _vm("v1", "proxmox")]
    pairs = vm_manager.bind_platforms(config, vms)  # type: ignore[arg-type]

    assert [vm.name for vm, _ in pairs] == ["v1", "v2"]  # name dedup
    assert pairs[0][1] is pairs[1][1]  # shared instance per site
    assert len(resolve_counter) == 1


def test_bind_platforms_union_spans_sites(
    make_config, resolve_counter: list[object]
) -> None:
    """A mixed-site batch still resolves once: the union of both sites'
    declared secrets goes through a single pass."""
    config = make_config(PROXMOX_SECTION)
    vms = [_vm("v1", "lima-local"), _vm("v2", "proxmox")]
    pairs = vm_manager.bind_platforms(config, vms)  # type: ignore[arg-type]

    assert len(pairs) == 2
    assert len(resolve_counter) == 1
    assert resolve_counter[0] == ["proxmox-token"]


def test_env_targets_join_the_site_secret_pass(
    make_config, resolve_counter: list[object], monkeypatch: pytest.MonkeyPatch
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

    from typing import cast

    assert len(resolve_counter) == 1
    assert sorted(cast("list[str]", resolve_counter[0])) == [
        "api-key",
        "proxmox-token",
    ]
    assert resolver.get("api-key") == "k"
    assert resolver.get("proxmox-token") == "pve-token"


def test_bind_platforms_empty_set_builds_no_registry(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch commands with an empty VM set (e.g. `session stop --all`
    matching nothing) must stay a complete no-op."""
    import agentworks.bootstrap as bootstrap

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("build_registry must not run for an empty VM set")

    monkeypatch.setattr(bootstrap, "build_registry", _boom)
    assert vm_manager.bind_platforms(make_config(), []) == []
