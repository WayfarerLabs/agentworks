"""Site resolution: the only constructor of bound platform instances,
plus the stranded-site ConfigError and validate_sites.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.errors import ConfigError, StateError
from agentworks.manifests import builtin as builtin_manifests
from agentworks.resources import Origin, Registry
from agentworks.vms import platforms as vm_platforms
from agentworks.vms.platforms.lima import LimaPlatform
from agentworks.vms.platforms.proxmox import ProxmoxPlatform
from agentworks.vms.sites import (
    VMSiteDecl,
    platform_for,
    resolve_site,
    site_manifest_hint,
    validate_sites,
)


def _registry(*sites: VMSiteDecl) -> Registry:
    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    vm_platforms.publish_to(registry)
    for site in sites:
        registry.add("vm-site", site.name, site, Origin.built_in(source="test"))
    registry.finalize()
    return registry


def test_resolve_site_binds_the_platform_config() -> None:
    registry = _registry(
        VMSiteDecl(
            name="gpu-box",
            platform="lima",
            platform_config={"vm_host": "me@box"},
        )
    )
    platform = resolve_site("gpu-box", registry)
    assert isinstance(platform, LimaPlatform)
    assert platform.site_name == "gpu-box"
    assert platform.is_remote
    assert platform.shared_backend(platform.platform_config)


def test_resolve_site_bundled_lima_is_local() -> None:
    registry = _registry()
    platform = resolve_site("lima", registry)
    assert isinstance(platform, LimaPlatform)
    assert not platform.is_remote


def test_resolve_site_unknown_raises_the_stranded_error() -> None:
    registry = _registry()
    with pytest.raises(ConfigError, match="site 'gpu-box' is not declared") as exc:
        resolve_site("gpu-box", registry)
    assert "kind: vm-site" in (exc.value.hint or "")
    assert "name: gpu-box" in (exc.value.hint or "")


def test_platform_for_resolves_through_the_vm_site() -> None:
    registry = _registry()
    vm = SimpleNamespace(site="wsl2")
    platform = platform_for(vm, registry)
    assert platform.name == "wsl2"


def test_secret_values_thread_to_the_bound_platform() -> None:
    registry = _registry(
        VMSiteDecl(
            name="px",
            platform="proxmox",
            platform_config={
                "api_url": "https://pve:8006",
                "node": "pve1",
                "token_id": "t",
                "template_vmid": 9000,
            },
        )
    )
    bound = resolve_site("px", registry, secret_values={"proxmox-token-secret": "s3cret"})
    assert isinstance(bound, ProxmoxPlatform)
    assert bound._api is not None

    unbound = resolve_site("px", registry)
    assert isinstance(unbound, ProxmoxPlatform)
    with pytest.raises(StateError, match="proxmox-token-secret"):
        _ = unbound._api


def test_validate_sites_accepts_declared_and_absent() -> None:
    registry = _registry()
    config = SimpleNamespace(defaults=SimpleNamespace(site=None))
    validate_sites(config, registry)  # type: ignore[arg-type]
    config = SimpleNamespace(defaults=SimpleNamespace(site="lima"))
    validate_sites(config, registry)  # type: ignore[arg-type]


def test_validate_sites_rejects_unknown_with_config_vocabulary() -> None:
    registry = _registry()
    config = SimpleNamespace(defaults=SimpleNamespace(site="nope"))
    with pytest.raises(ConfigError, match="defaults.site names an unknown site"):
        validate_sites(config, registry)  # type: ignore[arg-type]


def test_site_manifest_hint_carries_the_vm_host() -> None:
    hint = site_manifest_hint("gpu-box", vm_host="me@box")
    assert "name: gpu-box" in hint
    assert "vm_host: me@box" in hint
