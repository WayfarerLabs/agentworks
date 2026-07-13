"""Site resolution: the only constructor of bound platform instances,
plus the stranded-site ConfigError and validate_sites.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.errors import ConfigError, StateError
from agentworks.manifests import builtin as builtin_manifests
from agentworks.resources import Origin, Registry
from agentworks.vms.sites import (
    VMSiteDecl,
    platform_for,
    resolve_site,
    site_manifest_hint,
    validate_sites,
)


def _registry(*sites: VMSiteDecl) -> Registry:
    from tests.conftest import publish_all_platforms

    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    # Bypass the host-support gate: dispatch tests exercise resolution
    # shape, not this host's OS/tooling.
    publish_all_platforms(registry)
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
    platform = resolve_site("lima-local", registry)
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


def test_resolver_threads_to_the_bound_platform() -> None:
    """Construction registers the site's declared config secret on the
    operation's resolver and ops read the value from its cache; a
    platform constructed WITHOUT a resolver (direct/inspection use)
    fails ops with a typed error rather than resolving anything."""
    from typing import cast

    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.resolver import Resolver

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

    class _StubResolver:
        def __init__(self) -> None:
            self.registered: list[str] = []

        def register_name(self, name: str) -> SecretDecl:
            self.registered.append(name)
            return SecretDecl(name=name, description="")

        def get(self, name: str) -> str:
            assert name == "proxmox-token-secret"
            return "s3cret"

    stub = _StubResolver()
    bound = resolve_site("px", registry, resolver=cast(Resolver, stub))
    assert isinstance(bound, ProxmoxPlatform)
    # Construction registered the declared token secret for the
    # operation's boundary resolve.
    assert stub.registered == ["proxmox-token-secret"]
    assert bound._api is not None

    unbound = resolve_site("px", registry)
    assert isinstance(unbound, ProxmoxPlatform)
    with pytest.raises(StateError, match="proxmox-token-secret"):
        _ = unbound._api


def test_validate_sites_accepts_declared_and_absent() -> None:
    registry = _registry()
    config = SimpleNamespace(defaults=SimpleNamespace(site=None))
    validate_sites(config, registry)  # type: ignore[arg-type]
    config = SimpleNamespace(defaults=SimpleNamespace(site="lima-local"))
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


# -- select_site: the house selection model ---------------------------------


def test_select_site_flag_then_default_win() -> None:
    from agentworks.vms.sites import select_site

    registry = _registry()
    assert select_site("flagged", "defaulted", registry) == "flagged"
    assert select_site(None, "defaulted", registry) == "defaulted"


def test_select_site_infers_the_single_declared_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly one declared site: use it silently (the zero-config case
    -- a default install has only its host's bundled site)."""
    from agentworks.resources import Registry
    from agentworks.vms.sites import select_site
    from tests.conftest import publish_all_platforms

    registry = Registry.empty()
    publish_all_platforms(registry)
    registry.add(
        "vm-site",
        "only-one",
        VMSiteDecl(name="only-one", platform="lima"),
        Origin.built_in(source="test"),
    )
    registry.finalize()
    assert select_site(None, None, registry) == "only-one"


def test_select_site_prompts_between_several_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import output
    from agentworks.vms.sites import select_site

    registry = _registry()  # bundled lima-local + wsl2
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    captured: dict[str, object] = {}

    def _choose(msg: str, options: list[str]) -> int:
        captured["options"] = options
        return 1

    monkeypatch.setattr(output, "choose", _choose)
    assert select_site(None, None, registry) == "wsl2"
    assert captured["options"] == ["lima-local", "wsl2"]


def test_select_site_errors_between_several_when_non_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import output
    from agentworks.errors import ValidationError
    from agentworks.vms.sites import select_site

    registry = _registry()
    monkeypatch.setattr(output, "is_interactive", lambda: False)
    with pytest.raises(ValidationError, match="multiple sites") as exc:
        select_site(None, None, registry)
    assert "--site" in (exc.value.hint or "")


def test_select_site_errors_when_none_declared() -> None:
    from agentworks.errors import ValidationError
    from agentworks.resources import Registry
    from agentworks.vms.sites import select_site
    from tests.conftest import publish_all_platforms

    registry = Registry.empty()
    publish_all_platforms(registry)
    registry.finalize()
    with pytest.raises(ValidationError, match="no vm-sites are declared"):
        select_site(None, None, registry)


# -- lookup_site: bundled-miss hints ----------------------------------------


def test_lookup_bundled_site_miss_names_the_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VM on a bundled site whose platform requirements went away
    (limactl uninstalled) gets the platform's stated reason, never the
    misleading paste-a-manifest hint."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.resources import Registry
    from agentworks.vms.sites import lookup_site
    from tests.conftest import publish_all_platforms

    monkeypatch.setattr(
        LimaPlatform,
        "bundled_site_unsupported_reason",
        classmethod(lambda cls: "limactl not installed"),
    )
    registry = Registry.empty()
    publish_all_platforms(registry)
    registry.finalize()

    with pytest.raises(ConfigError, match="bundled site 'lima-local' is unavailable") as exc:
        lookup_site("lima-local", registry)
    assert "limactl" in str(exc.value)
    assert "kind: vm-site" not in (exc.value.hint or "")
