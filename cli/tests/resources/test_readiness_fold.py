"""The readiness fold and readiness-gated materialization (phase 3).

Pins the fold's verdicts (LLD c), the B1 property (the fold is total over a
malformed ``platform_config`` because ``not_ready`` never constructs), the
readiness gating of the finalize ``validate`` pass (R9.4) and of
materialization (R12, for both the not-ready and the disabled referrer), the
enablement-axis DISTRIBUTION through the fold end to end via the
``_node_enablement`` seam (R7), and that readiness is independent of validity
(R5).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.errors import ConfigError
from agentworks.resources.graph import DependencyState, Enablement, Readiness
from agentworks.resources.origin import Origin
from agentworks.resources.registry import Registry
from agentworks.vms.sites import VMSiteDecl


def _finalized(*sites: VMSiteDecl) -> Registry:
    """A finalized registry with every vm-platform row published and the
    given sites added (operator origin)."""
    from agentworks.capabilities import vm_platform as vp

    registry = Registry.empty()
    vp.publish_to(registry)
    for decl in sites:
        registry.add("vm-site", decl.name, decl, Origin.operator_declared(file=Path("sites.yaml"), line=1))
    registry.finalize()
    return registry


def _present(registry: Registry, kind: str, name: str) -> bool:
    return any(n == name for n, _ in registry.iter_kind_items(kind))


# -- The fixture disabled node (R7): the enablement axis, modeled --------------


def test_disabled_platform_dependency_propagates_enable_its_unit() -> None:
    """The enablement branch of ``vm-site.not_ready``: a DISABLED platform
    dependency yields the "enable its unit" hint read off the disabled node's
    own state. No producer ships a disabled node this effort, so the axis is
    proven by handing ``not_ready`` a disabled ``DependencyState`` directly
    (the fold's job is only to distribute these states)."""
    site = VMSiteDecl(name="x", platform="lima", platform_config={})
    deps: dict[tuple[str, str], DependencyState] = {
        ("vm-platform", "lima"): DependencyState(
            enablement=Enablement.disabled,
            readiness=None,  # None iff disabled
            impl=None,
        )
    }
    verdict = site.not_ready(deps)
    assert not verdict.is_ready
    assert verdict.reason == "depends on vm-platform 'lima', which is disabled; enable its unit"


def test_not_ready_platform_dependency_propagates_its_reason() -> None:
    """An enabled-but-not-ready platform propagates its reason into the
    site's verdict (the self-determined single-platform AND)."""
    site = VMSiteDecl(name="x", platform="wsl2", platform_config={})
    deps: dict[tuple[str, str], DependencyState] = {
        ("vm-platform", "wsl2"): DependencyState(
            enablement=Enablement.enabled,
            readiness=Readiness.blocked("Windows only"),
            impl=None,
        )
    }
    verdict = site.not_ready(deps)
    assert verdict.reason == "platform 'wsl2' is disabled: Windows only"


def test_disabled_platform_node_folds_end_to_end_to_enable_its_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """R7 end-to-end: the FOLD distributes a disabled dependency's state, not
    just the leaf ``not_ready`` logic. A registry is finalized with the lima
    platform node injected DISABLED via the ``_node_enablement`` seam; the
    fold then hands the (remote-ready) site a disabled platform
    ``DependencyState``, and the site's stored ``readiness_of`` verdict, the
    graph's source of truth, is the "enable its unit" hint. vm_host is set so
    the site would be ready if the platform were enabled, isolating the
    enablement propagation from the tool check. This is the exact seam the
    plugin rebuild fills."""
    from agentworks.capabilities import vm_platform as vp

    registry = Registry.empty()
    vp.publish_to(registry)
    registry.add(
        "vm-site",
        "s",
        VMSiteDecl(name="s", platform="lima", platform_config={"vm_host": "me@box"}),
        Origin.operator_declared(file=Path("sites.yaml"), line=1),
    )
    base = registry._node_enablement

    def _with_disabled_lima() -> dict[tuple[str, str], Enablement]:
        m = base()
        m[("vm-platform", "lima")] = Enablement.disabled
        return m

    monkeypatch.setattr(registry, "_node_enablement", _with_disabled_lima)
    registry.finalize()

    verdict = registry.graph.readiness_of("vm-site", "s")
    assert verdict.reason == "depends on vm-platform 'lima', which is disabled; enable its unit"


def test_disabled_secret_backend_is_excluded_from_the_active_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """R7 / LLD d: the enablement seam reaches secret resolution too. A
    present-but-DISABLED ``secret-backend`` (onepassword injected disabled via
    the ``_node_enablement`` seam) is dormant: ``active_backends`` excludes it
    from the chain it builds, so resolution never attempts it, exactly as an
    absent-from-chain backend is excluded, and WITHOUT a readiness warning (it
    is an opt-out, not a can't-run-here). ``enablement_of`` reports it disabled
    while its fold readiness stays a ready placeholder. Inert today (no producer
    ships), this is the axis the plugin rebuild fills."""
    from types import SimpleNamespace
    from typing import cast

    from agentworks.config import Config
    from agentworks.secrets import backends as secret_backends
    from agentworks.secrets.resolve import active_backends

    registry = Registry.empty()
    secret_backends.publish_to(registry)
    base = registry._node_enablement

    def _with_disabled_onepassword() -> dict[tuple[str, str], Enablement]:
        m = base()
        m[("secret-backend", "onepassword")] = Enablement.disabled
        return m

    monkeypatch.setattr(registry, "_node_enablement", _with_disabled_onepassword)
    registry.finalize()

    # The enablement axis reads disabled; the fold still stored a ready
    # placeholder (enablement, not readiness, answers for a disabled node).
    assert registry.graph.enablement_of("secret-backend", "onepassword") is Enablement.disabled
    assert registry.graph.readiness_of("secret-backend", "onepassword").is_ready

    config = cast("Config", SimpleNamespace(secret_config_data=SimpleNamespace(backends=("onepassword", "prompt"))))
    chain = [b.name for b in active_backends(config, registry)]
    assert chain == ["prompt"]  # onepassword excluded (disabled), never built into an ActiveBackend


# -- B1: the fold is total over a malformed block ------------------------------


def test_fold_does_not_throw_on_malformed_platform_config() -> None:
    """B1 (the ready side): a READY site with a malformed ``platform_config``
    surfaces the bad block as a clean ConfigError from the finalize VALIDATE
    pass (R5), carrying that pass's origin framing (``sites.yaml``). This alone
    cannot fully pin B1: if the fold DID construct, construction would raise the
    SAME validate ConfigError. The load-bearing B1 pin is
    ``test_r9_4_not_ready_site_malformed_block_is_deferred`` below, where a
    not-ready site's malformed block is folded WITHOUT validating (a
    constructing fold would throw there, but the non-constructing one does
    not). Here we additionally assert the origin framing to prove the error
    came from the validate pass, not from a mid-fold construction."""
    # vm_host as an int: lima.not_ready sees it truthy -> ready (no throw, no
    # construction); validate then rejects the non-string.
    site = VMSiteDecl(name="bad", platform="lima", platform_config={"vm_host": 123})
    with pytest.raises(ConfigError, match="vm_host must be a non-empty SSH host") as exc:
        _finalized(site)
    # The validate pass re-attaches the resource origin; construction would not.
    assert "sites.yaml" in str(exc.value)


def test_r5_ready_site_with_unknown_field_fails_validation() -> None:
    """R5: readiness is not validity. A ready site (lima is supported
    everywhere; no vm_host but we make it ready) with an unknown config field
    still fails the validate pass."""
    site = VMSiteDecl(name="bad", platform="lima", platform_config={"vm_host": "me@box", "bogus": "x"})
    with pytest.raises(ConfigError, match="unknown lima platform field"):
        _finalized(site)


def test_r9_4_not_ready_site_malformed_block_is_deferred(monkeypatch: pytest.MonkeyPatch) -> None:
    """R9.4: a NOT-ready site's malformed block is deferred, not validated.
    With no local ``limactl`` the local-lima site is not-ready, so its unknown
    config field is never validated and finalize does not raise (it would if
    the block were validated: see R5 above). Exercises the real
    non-constructing ``not_ready`` returning blocked."""
    monkeypatch.setattr("shutil.which", lambda name: None)  # no limactl -> not-ready
    site = VMSiteDecl(name="local", platform="lima", platform_config={"bogus": "x"})
    registry = _finalized(site)  # no raise: the block is deferred
    assert registry.graph.readiness_of("vm-site", "local").reason == "limactl not installed"


def test_r5_not_ready_site_with_valid_block_stays_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """R5, the other direction: a not-ready site with a perfectly valid block
    is still not-ready (a dependency/host verdict, blind to validity)."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    site = VMSiteDecl(name="local", platform="lima", platform_config={})  # valid (empty) block
    registry = _finalized(site)
    assert not registry.graph.is_ready("vm-site", "local")


# -- R12: readiness-gated materialization via site -> secret edges -------------


def test_r12_ready_site_materializes_its_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A READY site's config-implied secret materializes (auto-declares)."""
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    monkeypatch.setattr(ProxmoxPlatform, "not_ready", classmethod(lambda cls, config: Readiness.ready()))
    site = VMSiteDecl(
        name="ready-px",
        platform="proxmox",
        platform_config={
            "api_url": "https://pve:8006",
            "node": "pve1",
            "token_id": "t",
            "template_vmid": 9000,
            "token_secret": "tok-ready",
        },
    )
    registry = _finalized(site)
    assert _present(registry, "secret", "tok-ready")


def test_r12_not_ready_site_secret_does_not_materialize(monkeypatch: pytest.MonkeyPatch) -> None:
    """R12: a NOT-ready site's config-implied secret stays absent. The site's
    secret edge is emitted (suppression removed), but readiness gates
    materialization, so the would-be secret never enters the registry, exactly
    the suppression's old behavior by a different mechanism."""
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    monkeypatch.setattr(ProxmoxPlatform, "not_ready", classmethod(lambda cls, config: Readiness.blocked("sick")))
    site = VMSiteDecl(
        name="sick-px",
        platform="proxmox",
        platform_config={
            "api_url": "https://pve:8006",
            "node": "pve1",
            "token_id": "t",
            "template_vmid": 9000,
            "token_secret": "tok-sick",
        },
    )
    registry = _finalized(site)
    assert not registry.graph.is_ready("vm-site", "sick-px")
    assert not _present(registry, "secret", "tok-sick")


def test_r12_secret_referenced_by_both_ready_and_not_ready_materializes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLD b subtlety 4: a secret referenced by BOTH a ready and a not-ready
    site materializes (a ready node needs it) and its inbound
    ``dependents_of`` records BOTH referrers (readiness gates materialization,
    not reference attribution). Readiness keys on the site's config, so one
    proxmox site is ready and one is not, both naming the same token."""
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    def _readiness(cls: type, config: dict[str, object]) -> Readiness:
        return Readiness.blocked("sick") if config.get("node") == "SICK" else Readiness.ready()

    monkeypatch.setattr(ProxmoxPlatform, "not_ready", classmethod(_readiness))
    common = {"api_url": "https://pve:8006", "token_id": "t", "template_vmid": 9000, "token_secret": "shared-token"}
    ready = VMSiteDecl(name="ready-px", platform="proxmox", platform_config={**common, "node": "pve1"})
    sick = VMSiteDecl(name="sick-px", platform="proxmox", platform_config={**common, "node": "SICK"})
    registry = _finalized(ready, sick)

    assert _present(registry, "secret", "shared-token")
    sources = {entry.source for entry in registry.graph.dependents_of("secret", "shared-token")}
    assert ("vm-site", "ready-px") in sources
    assert ("vm-site", "sick-px") in sources


def test_r12_disabled_referrer_does_not_materialize_its_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """R12 for the DISABLED case: a disabled node's config-implied secret does
    not materialize either, even when the node's own readiness would be ready.
    ``has_ready_referrer`` must not count a disabled referrer (its readiness is
    a placeholder / not computed). The site is injected disabled via the
    ``_node_enablement`` seam; its proxmox platform ``not_ready`` is ready, so
    only enablement keeps the token out of the registry."""
    from agentworks.capabilities import vm_platform as vp
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    monkeypatch.setattr(ProxmoxPlatform, "not_ready", classmethod(lambda cls, config: Readiness.ready()))
    registry = Registry.empty()
    vp.publish_to(registry)
    registry.add(
        "vm-site",
        "off-px",
        VMSiteDecl(
            name="off-px",
            platform="proxmox",
            platform_config={
                "api_url": "https://pve:8006",
                "node": "pve1",
                "token_id": "t",
                "template_vmid": 9000,
                "token_secret": "tok-off",
            },
        ),
        Origin.operator_declared(file=Path("sites.yaml"), line=1),
    )
    base = registry._node_enablement

    def _with_disabled_site() -> dict[tuple[str, str], Enablement]:
        m = base()
        m[("vm-site", "off-px")] = Enablement.disabled
        return m

    monkeypatch.setattr(registry, "_node_enablement", _with_disabled_site)
    registry.finalize()

    assert not _present(registry, "secret", "tok-off")
