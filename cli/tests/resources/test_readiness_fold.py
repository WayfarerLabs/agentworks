"""The readiness fold and readiness-gated materialization (phase 3).

Pins the fold's verdicts (LLD c), the B1 property (the fold is total over a
malformed ``platform_config`` because ``not_ready`` never constructs), the
INDEPENDENCE of the finalize ``validate`` pass from both readiness and
enablement (a not-ready resource's block, and a secret's mapping to a disabled
backend, are validated like any other; what config is valid is the declared
model's answer, not the host's), the readiness gating of
materialization (R12, for both the not-ready and the disabled referrer), the
enablement-axis DISTRIBUTION through the fold end to end via the injected
enablement-source seam (R7/R13), and that readiness is independent of validity
(R5).

The disabled-node cases inject a stub :func:`_source_disabling` through the
shipped ``finalize(enablement_sources=...)`` seam (the refactor's original cases
monkeypatched the removed ``_node_enablement`` method; the fold behavior they
pin is unchanged, only the mechanism migrated). The plugin source's specific
"enable plugin `<name>`" reason and the multi-source composition are pinned in
``tests/plugins/test_enablement_producer.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ConfigError
from agentworks.origin import Origin
from agentworks.resources.graph import DependencyState, DisabledMark, Enablement, Readiness
from agentworks.resources.registry import Registry
from agentworks.schema import CapabilityBlock
from agentworks.vms.sites import VMSiteDecl

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.graph import EnablementSource


def _source_disabling(*keys: tuple[str, str], reason: str = "enable its unit") -> EnablementSource:
    """A stub enablement source disabling ``keys`` with a generic ``reason``.

    Injected via ``finalize(enablement_sources=[...])`` to drive the fold's
    distribution of a disabled dependency through the REAL shipped seam. The
    default ``reason`` reproduces the pre-migration verdicts byte-for-byte (the
    old hardcoded "enable its unit" tail); a specific reason is exercised by the
    plugin-source tests. Reads the present rows like a real source, so it marks
    only nodes that exist.
    """

    def _source(resources: Mapping[str, Mapping[str, object]]) -> dict[tuple[str, str], DisabledMark]:
        return {
            (kind, name): DisabledMark(reason=reason, source="test-stub")
            for kind, name in keys
            if name in resources.get(kind, {})
        }

    return _source


def _finalized(*sites: VMSiteDecl) -> Registry:
    """A finalized registry with every vm-platform row published and the
    given sites added (operator origin)."""
    from agentworks.capabilities.descriptor import descriptor_for
    from agentworks.capabilities.publish import publish_capability_rows

    registry = Registry.empty()
    publish_capability_rows(registry, descriptor_for("vm-platform"))
    for decl in sites:
        registry.add("vm-site", decl.name, decl, Origin.operator_declared(file=Path("sites.yaml"), line=1))
    registry.finalize()
    return registry


def _finalized_with_proxmox(
    *sites: VMSiteDecl,
    extra_disabled: tuple[tuple[str, str], ...] = (),
) -> Registry:
    """Like :func:`_finalized`, but with the ``proxmox`` platform published
    through its opt-in plugin and enabled.

    ``proxmox`` is the only platform that carries a config-implied secret, so
    the R12 materialization-gating cases below need it. Since Phase 10 (R11)
    proxmox ships in the ``proxmox`` system plugin, so its ``vm-platform`` row
    comes from ``publish_plugins`` (with a ``system-plugin`` origin), not the
    core publisher. The real ``plugin_enablement_source`` bound to
    ``enabled_system_plugins=("proxmox",)`` keeps proxmox enabled while disabling the
    other shipped plugins' rows (including claude's weak install-command row, so
    no weak row survives finalize unmarked). ``extra_disabled`` layers a stub
    source on top for a case that disables a specific node directly."""
    from types import SimpleNamespace
    from typing import cast

    from agentworks.capabilities.descriptor import descriptor_for
    from agentworks.capabilities.publish import publish_capability_rows
    from agentworks.config import Config
    from agentworks.plugins import publish_plugins
    from agentworks.plugins.enablement import plugin_enablement_source

    registry = Registry.empty()
    publish_capability_rows(registry, descriptor_for("vm-platform"))
    config = cast("Config", SimpleNamespace(enabled_system_plugins=("proxmox",)))
    publish_plugins(registry, config)
    for decl in sites:
        registry.add("vm-site", decl.name, decl, Origin.operator_declared(file=Path("sites.yaml"), line=1))
    sources: list[EnablementSource] = [plugin_enablement_source(config)]
    if extra_disabled:
        sources.append(_source_disabling(*extra_disabled))
    registry.finalize(enablement_sources=sources)
    return registry


def _present(registry: Registry, kind: str, name: str) -> bool:
    return any(n == name for n, _ in registry.iter_kind_items(kind))


# -- The fixture disabled node (R7): the enablement axis, modeled --------------


def test_not_ready_platform_dependency_propagates_its_reason() -> None:
    """An enabled-but-not-ready platform propagates its verdict verbatim into
    the site's verdict (the self-determined single-platform AND). The platform's
    readiness reason already names it ("platform 'wsl2' is unsupported here:
    ..."), so the site passes it through rather than re-wrapping (which would
    double the naming); the surface adds the "Not ready:" framing (R9.1)."""
    site = VMSiteDecl(name="x", platform=CapabilityBlock.of("wsl2", **{}))
    deps: dict[tuple[str, str], DependencyState] = {
        ("vm-platform", "wsl2"): DependencyState(
            enablement=Enablement.enabled,
            readiness=Readiness.blocked("platform 'wsl2' is unsupported here: Windows only"),
            impl=None,
        )
    }
    verdict = site.not_ready(deps)
    assert verdict.reason == "platform 'wsl2' is unsupported here: Windows only"


def test_disabled_platform_node_folds_end_to_end_to_enable_its_unit() -> None:
    """R7: the enablement branch of ``vm-site.not_ready``, reached the way
    production reaches it.

    The FOLD distributes a disabled dependency's state, not just the leaf
    ``not_ready`` logic, so going end to end covers both and handing
    ``not_ready`` a hand-built disabled ``DependencyState`` covers only the
    leaf. A registry is finalized with the lima
    platform node injected DISABLED via a stub enablement source; the fold then
    hands the (remote-ready) site a disabled platform ``DependencyState``, and
    the site's stored ``readiness_of`` verdict, the graph's source of truth, is
    the disabled hint. The ssh placement is set so the site would be ready if the
    platform were enabled, isolating the enablement propagation from the tool
    check. The
    stub's default reason reproduces the original "enable its unit" verdict; the
    plugin source's specific reason is pinned in the producer tests."""
    from agentworks.capabilities.descriptor import descriptor_for
    from agentworks.capabilities.publish import publish_capability_rows

    registry = Registry.empty()
    publish_capability_rows(registry, descriptor_for("vm-platform"))
    registry.add(
        "vm-site",
        "s",
        VMSiteDecl(name="s", platform=CapabilityBlock.of("lima", placement={"mode": "ssh", "host": "me@box"})),
        Origin.operator_declared(file=Path("sites.yaml"), line=1),
    )
    registry.finalize(enablement_sources=[_source_disabling(("vm-platform", "lima"))])

    verdict = registry.graph.readiness_of("vm-site", "s")
    assert verdict.reason == "depends on vm-platform 'lima', which is disabled; enable its unit"


def test_disabled_secret_backend_makes_its_active_source_not_ready() -> None:
    """A configured source backed by a disabled plugin stays in the typed chain
    with a folded not-ready verdict, so resolution can skip it without ever
    constructing the backend client."""
    from types import SimpleNamespace
    from typing import cast

    from agentworks.capabilities.descriptor import descriptor_for
    from agentworks.capabilities.publish import publish_capability_rows
    from agentworks.config import Config
    from agentworks.plugins import publish_plugins
    from agentworks.plugins.enablement import plugin_enablement_source
    from agentworks.secrets.resolve import active_sources
    from agentworks.secrets.sources import SecretSourceDecl, publish_builtin_secret_sources

    registry = Registry.empty()
    publish_capability_rows(registry, descriptor_for("secret-backend"))
    publish_builtin_secret_sources(registry)
    # onepassword ships as a system plugin now (its built-in row is gone), so
    # publish its capability row and finalize it with the same opt-in source
    # production supplies.
    plugin_config = cast("Config", SimpleNamespace(enabled_system_plugins=()))
    publish_plugins(registry, plugin_config)
    registry.add(
        "secret-source",
        "onepassword",
        SecretSourceDecl(name="onepassword", backend=CapabilityBlock.of("onepassword")),
        Origin.operator_declared(file=Path("sources.yaml"), line=1),
    )
    registry.finalize(enablement_sources=[plugin_enablement_source(plugin_config)])

    # The enablement axis reads disabled; the fold still stored a ready
    # placeholder (enablement, not readiness, answers for a disabled node).
    assert registry.graph.enablement_of("secret-backend", "onepassword") is Enablement.disabled
    assert registry.graph.readiness_of("secret-backend", "onepassword").is_ready

    config = cast("Config", SimpleNamespace(secret_config_data=SimpleNamespace(sources=("onepassword", "prompt"))))
    sources = active_sources(config, registry)
    assert [source.name for source in sources] == ["onepassword", "prompt"]
    assert not sources[0].readiness.is_ready  # retained so typed resolution can report why it was skipped


@pytest.mark.parametrize("disable_onepassword", [True, False], ids=["disabled", "enabled"])
def test_r9_9_mapping_is_validated_whether_or_not_its_backend_is_enabled(disable_onepassword: bool) -> None:
    """R9.9: the finalize ``validate`` pass validates a secret's mapping
    against its backend's model regardless of the backend's ENABLEMENT.

    A malformed onepassword mapping fails the build whether onepassword is
    enabled or injected disabled through the shipped enablement-source seam.
    Deferring the check to enablement time would let an operator accumulate
    mappings that all detonate the moment they turn the backend on.

    Non-vacuous on the disabled branch: it first finalizes the same shape with
    a VALID mapping and reads the enablement axis off the graph, so a stub
    source that stopped disabling could not leave this green by accident.
    onepassword stays PRESENT on both branches (only the mark moves), which is
    what keeps this off the absent-backend path, where no model exists to
    validate against and the dangling edge answers instead."""
    from types import SimpleNamespace
    from typing import cast

    from agentworks.capabilities.descriptor import descriptor_for
    from agentworks.capabilities.publish import publish_capability_rows
    from agentworks.config import Config
    from agentworks.plugins import publish_plugins
    from agentworks.plugins.enablement import plugin_enablement_source
    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.sources import SecretSourceDecl

    def _build(mapping: str) -> Registry:
        registry = Registry.empty()
        publish_capability_rows(registry, descriptor_for("secret-backend"))
        # onepassword's row now comes from the plugin path, not a built-in.
        plugin_config = cast("Config", SimpleNamespace(enabled_system_plugins=("onepassword",)))
        publish_plugins(registry, plugin_config)
        registry.add(
            "secret-source",
            "vault-op",
            SecretSourceDecl(name="vault-op", backend=CapabilityBlock.of("onepassword")),
            Origin.operator_declared(file=Path("sources.yaml"), line=1),
        )
        registry.add(
            "secret",
            "vaulted",
            SecretDecl(name="vaulted", description="a vaulted key", backend_mappings={"vault-op": mapping}),
            Origin.operator_declared(file=Path("c.toml"), line=1),
        )
        sources: list[EnablementSource] = [plugin_enablement_source(plugin_config)]
        if disable_onepassword:
            sources.append(_source_disabling(("secret-backend", "onepassword")))
        registry.finalize(enablement_sources=sources)
        return registry

    # The precondition, proven rather than assumed: onepassword is present, and
    # its axis reads whichever way this branch asked for.
    precondition = _build("op://Vault/Item/field")
    assert precondition.graph.enablement_of("secret-backend", "onepassword") is (
        Enablement.disabled if disable_onepassword else Enablement.enabled
    )

    # The malformed mapping fails on both branches.
    with pytest.raises(ConfigError, match="onepassword"):
        _build("not-an-op-uri")


# -- B1: the fold is total over a malformed block ------------------------------


def test_fold_does_not_throw_on_malformed_platform_config() -> None:
    """B1 (the ready side): a READY site with a malformed ``platform_config``
    surfaces the bad block as a clean ConfigError from the finalize VALIDATE
    pass (R5), carrying that pass's origin framing (``sites.yaml``).

    The origin framing is what carries the B1 signal here: a constructing fold
    would raise from construction, which attaches no origin, so ``sites.yaml``
    in the message proves the error came from the validate pass at 7 and not
    from a mid-fold construction at 4. The direct pin on the fold's totality is
    ``test_not_ready_is_total_over_malformed_config`` below, which hands
    ``not_ready`` blocks no model would accept; end-to-end deferral can no
    longer pin it, because validation is unconditional now and every malformed
    block raises somewhere."""
    # host as an int: lima.not_ready sees a non-local tag -> ready (no throw,
    # no construction); validate then rejects the non-string.
    site = VMSiteDecl(name="bad", platform=CapabilityBlock.of("lima", placement={"mode": "ssh", "host": 123}))
    with pytest.raises(ConfigError, match="placement.host: must be a string") as exc:
        _finalized(site)
    # The validate pass re-attaches the resource origin; construction would not.
    assert "sites.yaml" in str(exc.value)


def test_r5_ready_site_with_unknown_field_fails_validation() -> None:
    """R5: readiness is not validity. A ready site (lima is supported
    everywhere; an ssh placement needs no local limactl) with an unknown
    config field still fails the validate pass.

    The NOT-ready host is not a second test: the typo case below runs its
    whole assertion under both host states, which is where the direction
    R9.4 originally had backwards is pinned (deferring validation until a
    resource was ready meant a not-ready host silently accepted config a
    ready host refused, so what "valid" meant depended on the machine
    reading the document)."""
    site = VMSiteDecl(
        name="bad",
        platform=CapabilityBlock.of("lima", placement={"mode": "ssh", "host": "me@box"}, bogus="x"),
    )
    with pytest.raises(ConfigError, match="bogus: unknown field"):
        _finalized(site)


def test_typo_in_the_placement_host_is_refused_rather_than_changing_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect this ungating fixed, end to end, now closed a second time
    at the SHAPE: an ssh-placed lima site whose host key is misspelled.

    The original failure had two halves. Ungating the validate pass fixed
    the first: while it was readiness-gated, the not-ready verdict
    suppressed the very error naming the typo, so the operator was told
    ``limactl not installed``, a problem they did not have, and the
    identical document WAS refused on a host that happened to have
    ``limactl``. The required ``placement`` union fixes the second: a typo
    can no longer LOOK like a choice. ``not_ready`` keys on the tag saying
    ``local``, which an ssh-placed site never says, so the misspelling
    cannot fold the site to local at all, whatever the host.

    Both hosts must refuse it, and name the key at the address the
    operator wrote it."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    typo: dict[str, object] = {"placement": {"mode": "ssh", "hst": "me@box"}}
    for limactl in (None, "/usr/bin/limactl"):
        monkeypatch.setattr(
            "shutil.which",
            lambda name, found=limactl: "/usr/bin/ssh" if name == "ssh" else found,
        )
        # The SHAPE half, asserted on the hook the fold calls: the tag says
        # ssh, so the site is not local, so no limactl verdict is reachable
        # for it however this host is equipped. This is the half the
        # required union added; keying on the tag rather than on a key's
        # presence is what makes a misspelling unable to change the answer.
        assert LimaPlatform.not_ready(typo).is_ready

        site = VMSiteDecl(name="gpu", platform=CapabilityBlock.of("lima", **typo))
        with pytest.raises(ConfigError) as exc:
            _finalized(site)
        message = str(exc.value)
        # The VALIDATION half: the typo is named, at its real path, and the
        # REQUIRED host it displaced is named too. Two precise complaints,
        # neither of which is the misleading limactl verdict.
        assert "placement.hst: unknown field" in message
        assert "placement.host: is required" in message
        assert "limactl" not in message


def test_not_ready_is_total_over_malformed_config() -> None:
    """B1, pinned directly on the hook the fold calls: ``not_ready`` returns a
    verdict for blocks no model would accept, rather than raising.

    It is a classmethod over best-effort config and never constructs, so it
    cannot re-run the throwing construct-time validator. That is what keeps the
    fold total (R1/R4) and what stops a malformed block from becoming a
    permanent readiness reason (the R9.4 loop). Independent of the validate
    pass, which is why ungating that pass leaves this contract untouched."""
    from agentworks.capabilities.vm_platform.lima import LimaPlatform

    for config in (
        {"bogus": "x"},
        {"placement": 123},
        {"placement": {"mode": {"nested": "junk"}}},
        {"placement": {"mode": "local", "host": []}},
        {},
    ):
        assert LimaPlatform.not_ready(config).reason in (None, "limactl not installed")


def test_r5_not_ready_site_with_valid_block_stays_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """R5, the other direction: a not-ready site with a perfectly valid block
    is still not-ready (a dependency/host verdict, blind to validity)."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    site = VMSiteDecl(name="local", platform=CapabilityBlock.of("lima", placement={"mode": "local"}))
    registry = _finalized(site)
    assert not registry.graph.is_ready("vm-site", "local")


# -- R12: readiness-gated materialization via site -> secret edges -------------


def test_r12_ready_site_materializes_its_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A READY site's config-implied secret materializes (auto-declares)."""
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    monkeypatch.setattr(ProxmoxPlatform, "not_ready", classmethod(lambda cls, config: Readiness.ready()))
    site = VMSiteDecl(
        name="ready-px",
        platform=CapabilityBlock.model_validate(
            {
                "name": "proxmox",
                **{
                    "api_url": "https://pve:8006",
                    "node": "pve1",
                    "token_id": "t",
                    "template_vmid": 9000,
                    "token_secret": "tok-ready",
                },
            }
        ),
    )
    registry = _finalized_with_proxmox(site)
    assert _present(registry, "secret", "tok-ready")


def test_r12_not_ready_site_secret_does_not_materialize(monkeypatch: pytest.MonkeyPatch) -> None:
    """R12: a NOT-ready site's config-implied secret stays absent. The site's
    secret edge is emitted (suppression removed), but readiness gates
    materialization, so the would-be secret never enters the registry, exactly
    the suppression's old behavior by a different mechanism."""
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    monkeypatch.setattr(ProxmoxPlatform, "not_ready", classmethod(lambda cls, config: Readiness.blocked("sick")))
    site = VMSiteDecl(
        name="sick-px",
        platform=CapabilityBlock.model_validate(
            {
                "name": "proxmox",
                **{
                    "api_url": "https://pve:8006",
                    "node": "pve1",
                    "token_id": "t",
                    "template_vmid": 9000,
                    "token_secret": "tok-sick",
                },
            }
        ),
    )
    registry = _finalized_with_proxmox(site)
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
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    def _readiness(cls: type, config: dict[str, object]) -> Readiness:
        return Readiness.blocked("sick") if config.get("node") == "SICK" else Readiness.ready()

    monkeypatch.setattr(ProxmoxPlatform, "not_ready", classmethod(_readiness))
    common = {"api_url": "https://pve:8006", "token_id": "t", "template_vmid": 9000, "token_secret": "shared-token"}
    ready = VMSiteDecl(name="ready-px", platform=CapabilityBlock.of("proxmox", **{**common, "node": "pve1"}))
    sick = VMSiteDecl(name="sick-px", platform=CapabilityBlock.of("proxmox", **{**common, "node": "SICK"}))
    registry = _finalized_with_proxmox(ready, sick)

    assert _present(registry, "secret", "shared-token")
    sources = {entry.source for entry in registry.graph.dependents_of("secret", "shared-token")}
    assert ("vm-site", "ready-px") in sources
    assert ("vm-site", "sick-px") in sources


def test_r12_disabled_referrer_does_not_materialize_its_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """R12 for the DISABLED case: a disabled node's config-implied secret does
    not materialize either, even when the node's own readiness would be ready.
    ``has_ready_referrer`` must not count a disabled referrer (its readiness is
    a placeholder / not computed). The site is injected disabled via a stub
    enablement source; its proxmox platform ``not_ready`` is ready, so only
    enablement keeps the token out of the registry."""
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform

    monkeypatch.setattr(ProxmoxPlatform, "not_ready", classmethod(lambda cls, config: Readiness.ready()))
    # proxmox is enabled (its platform ready); only the direct disabling of the
    # off-px SITE keeps its token out of the registry.
    registry = _finalized_with_proxmox(
        VMSiteDecl(
            name="off-px",
            platform=CapabilityBlock.model_validate(
                {
                    "name": "proxmox",
                    **{
                        "api_url": "https://pve:8006",
                        "node": "pve1",
                        "token_id": "t",
                        "template_vmid": 9000,
                        "token_secret": "tok-off",
                    },
                }
            ),
        ),
        extra_disabled=(("vm-site", "off-px"),),
    )

    assert not _present(registry, "secret", "tok-off")
