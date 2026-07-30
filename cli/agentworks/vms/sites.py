"""VM sites: the declared resource that exposes a configured platform,
plus site resolution (the only constructor of platform instances).

A ``vm-site`` is "a configured place to create VMs" (ADR 0016's
instance-identity test): consumers name the site (``agw vm create
--site``, ``defaults.site``, ``vms.site`` provenance; never a
template: placement is host/operator-scoped), and one platform backs
many sites. Site rows arrive from the built-in bundle (``lima-local``,
``wsl2``), operator manifests, and the legacy ``[azure]`` /
``[proxmox]`` TOML sections.

Every site registers UNCONDITIONALLY (bundled and declared alike,
whatever the host). Whether it can run here is READINESS, computed by
the finalize fold and stored on the graph: a site is not-ready when its
platform is host-unsupported (wsl2 off Windows) or the bound config
reports a missing requirement (a local-Lima site without ``limactl``).
An UNKNOWN platform (a typo, or an uninstalled plugin) is no longer a
self-disable but a hard finalize error (R9.2): the site emits its
platform edge unconditionally and the absent capability row is a loud
miss. A not-ready site still lists, describes, and holds references;
using it (:func:`resolve_site`) is a typed error with the reason, and
existing references (VMs, ``defaults.site``) degrade to doctor warnings
rather than breaking every command. Readiness is folded once at finalize and
read off the graph (``graph.readiness_of``); :func:`select_site` /
:func:`ensure_site_ready` and the inspect / doctor projections all read that
stored verdict rather than recomputing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.declared_resource import DeclaredResource
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.resources.graph import BuildContext, DependencyState, Readiness
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry


@dataclass(frozen=True, kw_only=True)
class VMSiteDecl(DeclaredResource):
    """The declared ``vm-site`` resource.

    The internal representation follows the YAML manifest shape (ADR
    0016): ``platform`` names the capability; ``platform_config`` is
    the nested platform-owned blob. The flat legacy TOML sections
    (``[azure]`` / ``[proxmox]``) are the only place platform-owned
    fields sit at a top level; their loader nests at the boundary.
    """

    platform: str
    platform_config: dict[str, object] = field(default_factory=dict)

    def dependencies(self, context: BuildContext) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceRef,
        )
        from agentworks.resources.reference import sourced_references

        source = ("vm-site", self.name)
        # A site ALWAYS emits its platform edge (the suppression is gone,
        # R13/R12): the platform node is always present (published
        # unconditionally), so the edge always resolves; an unknown platform
        # is now a hard finalize miss (R9.2), not a silently-dropped edge.
        # Whether this site can run on the host is READINESS (the fold), and
        # readiness gates whether its config-implied secrets materialize (R12),
        # so the edges are emitted unconditionally here and gated downstream.
        from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

        refs: list[ResourceReference] = [
            _ResourceRef(
                name=self.platform,
                kind="vm-platform",
                usage="the VM platform",
                source=source,
            )
        ]
        capability = VM_PLATFORM_REGISTRY.get(self.platform)
        if capability is not None:
            # Capability-implied references: the platform derives the
            # references its config block implies (dependencies, total and
            # non-throwing); this resource (the config block's owner)
            # attributes them to itself via the shared sourced-conversion. An
            # unknown platform emits only the (dangling) platform edge above,
            # which the resolve pass turns into the R9.2 hard error.
            refs.extend(
                sourced_references(capability.dependencies(f"vm-site/{self.name}", self.platform_config), source)
            )
        return refs

    def not_ready(self, deps: Mapping[tuple[str, str], DependencyState]) -> Readiness:
        """This site's readiness verdict, self-determined from its single
        platform dependency's :class:`DependencyState` (the fold hands it in
        ``deps``) plus its own ``platform_config`` (LLD c). Pure, total,
        NON-CONSTRUCTING: the config-dependent tool check calls the platform's
        ``not_ready`` classmethod off the graph-carried impl, never building an
        instance (which would re-run the throwing validator: the B1 loop).

        The chain, by owner: a DISABLED platform yields the "enable its unit"
        hint read off its own state (R7; exercised only by the fixture, since
        nothing produces a disabled node this effort); a not-ready platform
        (host-unsupported) propagates its verdict verbatim (the platform's own
        readiness reason already names it, e.g. "platform 'wsl2' is unsupported
        here: Windows only", so re-wrapping would double the naming); otherwise
        the site re-asks with its OWN config (a remote-Lima site needs no local
        ``limactl``, so it does not blindly inherit the platform verdict, R4).
        The "disabled" branch is the opt-in axis (enablement), not host
        readiness; readiness reasons never say "disabled" (R6/R9.1).
        """
        from typing import cast

        from agentworks.resources.graph import Enablement, Readiness

        platform = deps[("vm-platform", self.platform)]
        if platform.enablement is Enablement.disabled:
            return Readiness.blocked(f"depends on vm-platform '{self.platform}', which is disabled; enable its unit")
        if platform.readiness is not None and not platform.readiness.is_ready:
            return platform.readiness
        if platform.impl is None:
            return Readiness.ready()
        # The impl is the platform CLASS the graph stamped (``_impl_for`` fails
        # fast on a missing impl), so ``not_ready`` is a classmethod call, never
        # a construction.
        return cast("type[VMPlatform]", platform.impl).not_ready(self.platform_config)

    def validate(self) -> None:
        """Throwing shape check for the ``platform_config`` blob, run by
        the finalize ``validate`` pass. Mirrors ``dependencies``:
        the named platform capability validates the blob it owns. An
        unknown platform is a no-op HERE (the platform capability is absent,
        so there is no blob owner to validate against); the site's dangling
        platform edge is what makes the unknown platform a hard finalize miss
        (R9.2). The blob is validated whenever the platform's implementation
        is seated, regardless of host support (an unsupported platform still
        validates an empty or well-formed blob for a ready+enabled site).
        """
        from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

        capability = VM_PLATFORM_REGISTRY.get(self.platform)
        if capability is not None:
            capability.validate(f"vm-site/{self.name}", self.platform_config)


def site_manifest_hint(name: str, *, vm_host: str | None = None) -> str:
    """A ready-to-paste vm-site manifest document for ``name``.

    Used by the stranded-VM ``ConfigError`` (a migrated remote-Lima row
    whose site manifest the operator has not added yet), the DB
    migration's printed snippets, and doctor.
    """
    config_lines = ""
    if vm_host is not None:
        config_lines = f"\n  platform_config:\n    vm_host: {vm_host}"
    return (
        "declare it under ~/.config/agentworks/resources/ (any filename), "
        "e.g.:\n\n"
        "apiVersion: agentworks/v1\n"
        "kind: vm-site\n"
        "metadata:\n"
        f"  name: {name}\n"
        "spec:\n"
        "  platform: lima"
        f"{config_lines}\n\n"
        "(adjust the platform and platform_config to match where this "
        "site's VMs actually live; see `agw resource sample vm-site`)"
    )


def select_site(
    flag: str | None,
    default_site: str | None,
    registry: Registry,
) -> str:
    """Site selection for ``vm create``: the explicit flag, then
    ``defaults.site``, then the house model over the READY sites:
    infer when exactly one exists, prompt interactively when several
    do, error otherwise. Not-ready sites are never a choice (using one
    is an error), but their existence never breaks inference.

    Placement is deliberately host/operator-scoped only: templates
    describe WHAT a VM is and carry no site (a shared template must not
    smuggle a per-host placement decision), and there is no hardcoded
    fallback site (sites not ready on this host drop out, so "exactly
    one ready" IS the zero-config case).
    """
    from agentworks import output
    from agentworks.errors import ValidationError

    if flag:
        return flag
    if default_site:
        return default_site
    graph = registry.graph
    sites = sorted(registry.iter_kind_items("vm-site"), key=lambda item: item[0])
    names = [name for name, _decl in sites if graph.is_ready("vm-site", name)]
    if len(names) == 1:
        return names[0]
    if not names:
        not_ready = [f"{name} ({graph.readiness_of('vm-site', name).reason})" for name, _decl in sites]
        detail = f" (not ready: {'; '.join(not_ready)})" if not_ready else ""
        raise ValidationError(
            f"no vm-sites are ready on this host{detail}",
            hint=(
                "meet a not-ready site's requirement, or declare a site "
                "under ~/.config/agentworks/resources/ "
                "(`agw resource sample vm-site`)"
            ),
        )
    if output.is_interactive():
        choice = output.choose("Select a site:", names)
        return names[choice]
    raise ValidationError(
        f"multiple sites are ready ({', '.join(names)})",
        hint="pass --site <name> or set defaults.site in config.toml",
    )


def lookup_site(name: str, registry: Registry) -> VMSiteDecl:
    """The site's declaration (ready or not-ready), or a
    ``ConfigError`` with the ready-to-paste manifest on a miss (the
    stranded-site case, e.g. a migrated remote-Lima row whose site
    manifest the operator has not added yet). Bundled sites register
    on every host, so a miss is never a host-requirement problem.
    """
    try:
        decl = registry.lookup("vm-site", name)
    except KeyError:
        raise ConfigError(
            f"site '{name}' is not declared",
            hint=site_manifest_hint(name),
        ) from None
    assert isinstance(decl, VMSiteDecl)
    return decl


def site_platform_name(site: str, registry: Registry) -> str:
    """The capability name backing ``site``, for consumers that surface
    it (``AGENTWORKS_PLATFORM``, ``vm describe``). Same stranded-site
    ``ConfigError`` as :func:`lookup_site` on an undeclared site.
    """
    return lookup_site(site, registry).platform


def resolve_site(
    name: str,
    registry: Registry,
) -> VMPlatform:
    """Resolve a site name to its constructed platform instance.

    Returns the platform class instantiated with the site's validated
    ``platform_config`` (construction is cheap and never resolves or
    prompts; the declared config secrets join the operation's boundary
    union through the holding node's ``secret_refs``). Manager code
    holds the bound platform and never sees ``VM_PLATFORM_REGISTRY``
    or platform classes.

    This is the one chokepoint every operation passes through, so the
    readiness guard lives here: using a not-ready site is a typed error
    naming the reason chain.
    """
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

    decl = lookup_site(name, registry)
    ensure_site_ready(decl, registry)
    # Ready implies the platform is installed and supported here.
    platform_cls = VM_PLATFORM_REGISTRY[decl.platform]
    return platform_cls(decl.name, decl.platform_config)


def ensure_site_ready(decl: VMSiteDecl, registry: Registry) -> None:
    """The typed using-a-not-ready-site error. ``resolve_site`` (the
    chokepoint every op passes through) always applies it; roots with
    operator interaction BEFORE their resolve (``create_vm``'s system
    slug prompt) call it up front too, so a not-ready explicit choice
    errors before the operator answers anything.

    Reads the site's stored readiness verdict off the graph (R11: the fold
    computed it, this does not recompute). The verdict folds in the platform's
    enablement too, so a site whose platform is disabled (the opt-in axis) is
    correctly unusable, reported with the "enable its unit" hint.
    """
    from agentworks.errors import StateError

    reason = registry.graph.readiness_of("vm-site", decl.name).reason
    if reason is not None:
        raise StateError(
            f"vm-site '{decl.name}' is not ready on this host: {reason}",
            hint=("`agw doctor` lists each site's state; meet the requirement or use a ready site"),
        )


def validate_sites(config: Config, registry: Registry) -> None:
    """Config consistency at the composition boundary (run by
    ``bootstrap.build_registry`` after finalize, beside
    ``secrets.validate_chain``): settings that name sites must resolve.

    Config vocabulary in the errors; settings are never published as
    pseudo-resources (ADR 0016).
    """
    site = config.defaults.site
    if site is None:
        return
    try:
        registry.lookup("vm-site", site)
    except KeyError:
        # Unknown only: a DISABLED site is valid config here (this
        # host may simply lack the requirement); using it errors at
        # resolve_site with the reason, and doctor warns on the
        # reference.
        raise ConfigError(
            f"defaults.site names an unknown site '{site}'",
            hint=(
                f"declare a vm-site named '{site}' "
                f"(see `agw resource sample vm-site`) or point defaults.site "
                f"at a declared site (`agw resource list --kind vm-site`)"
            ),
        ) from None
