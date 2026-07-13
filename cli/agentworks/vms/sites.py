"""VM sites: the declared resource that exposes a configured platform,
plus site resolution (the only constructor of platform instances).

A ``vm-site`` is "a configured place to create VMs" (ADR 0016's
instance-identity test): many consumers name the site
(``vm-template.spec.site``, ``agw vm create --site``, ``defaults.site``,
``vms.site`` provenance), and one platform backs many sites. Site rows
arrive from the built-in bundle (``lima``, ``wsl2``), operator
manifests, and the legacy ``[azure]`` / ``[proxmox]`` TOML sections.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError
from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry, ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.secrets.resolver import Resolver


@dataclass(frozen=True)
class VMSiteDecl:
    """The declared ``vm-site`` resource.

    The internal representation follows the YAML manifest shape (ADR
    0016): ``platform`` names the capability; ``platform_config`` is
    the nested platform-owned blob. The flat legacy TOML sections
    (``[azure]`` / ``[proxmox]``) are the only place platform-owned
    fields sit at a top level; their loader nests at the boundary.
    """

    name: str
    platform: str
    platform_config: dict[str, object] = field(default_factory=dict)
    description: str | None = None
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceRef,
        )
        from agentworks.resources.reference import SecretReference

        source = ("vm-site", self.name)
        refs: list[ResourceReference] = [
            # The platform field references the capability; the
            # framework's error miss policy catches typos uniformly.
            _ResourceRef(
                name=self.platform,
                kind="vm-platform",
                usage="the VM platform",
                source=source,
            ),
        ]
        # Capability-implied references: the platform validates its
        # config block and returns the references it implies; this
        # resource (the config block's owner) attributes them to itself.
        from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

        capability = VM_PLATFORM_REGISTRY.get(self.platform)
        if capability is not None:
            for cref in capability.validate_config(
                f"vm-site/{self.name}", self.platform_config
            ):
                ref_cls = SecretReference if cref.kind == "secret" else _ResourceRef
                refs.append(
                    ref_cls(
                        name=cref.name,
                        kind=cref.kind,
                        usage=cref.usage,
                        source=source,
                    )
                )
        return refs


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
    ``defaults.site``, then the house model over the declared sites --
    infer when exactly one exists, prompt interactively when several
    do, error otherwise.

    Placement is deliberately host/operator-scoped only: templates
    describe WHAT a VM is and carry no site (a shared template must not
    smuggle a per-host placement decision), and there is no hardcoded
    fallback site (the bundled sites publish only where viable, so
    "exactly one declared" IS the zero-config case).
    """
    from agentworks import output
    from agentworks.errors import ValidationError

    if flag:
        return flag
    if default_site:
        return default_site
    names = sorted(name for name, _ in registry.iter_kind_items("vm-site"))
    if len(names) == 1:
        return names[0]
    if not names:
        raise ValidationError(
            "no vm-sites are declared on this host",
            hint=(
                "declare one under ~/.config/agentworks/resources/ "
                "(`agw resource sample vm-site`), or install the tooling "
                "for a bundled site (limactl for lima-local; WSL on "
                "Windows for wsl2)"
            ),
        )
    if output.is_interactive():
        choice = output.choose("Select a site for this VM", names)
        return names[choice]
    raise ValidationError(
        f"multiple sites are declared ({', '.join(names)})",
        hint="pass --site <name> or set defaults.site in config.toml",
    )


def lookup_site(name: str, registry: Registry) -> VMSiteDecl:
    """The site's declaration, or a ``ConfigError`` whose hint matches
    the miss: a bundled site missing because its platform's host
    requirements aren't met gets the platform's stated reason (the
    paste-a-manifest hint would be actively misleading there); any
    other miss is the stranded-site case (e.g. a migrated remote-Lima
    row whose site manifest the operator has not added yet) and gets
    the ready-to-paste manifest.
    """
    try:
        decl = registry.lookup("vm-site", name)
    except KeyError:
        from agentworks.capabilities.vm_platform import (
            VM_PLATFORM_REGISTRY,
            bundled_site_platform,
        )

        platform_name = bundled_site_platform(name)
        if platform_name is not None:
            reason = VM_PLATFORM_REGISTRY[
                platform_name
            ].bundled_site_unsupported_reason()
            if reason is not None:
                raise ConfigError(
                    f"the bundled site '{name}' is unavailable on this "
                    f"host: {reason}",
                    hint=(
                        f"meet the requirement to get the site back, or "
                        f"use a different site (platform: {platform_name})"
                    ),
                ) from None
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


def site_shared_backend(decl: VMSiteDecl) -> bool:
    """Whether the site's backend is plausibly shared between
    agentworks installs (drives the deferred slug nudge). Declared
    by the platform; lima computes it from ``vm_host`` presence.
    """
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

    capability = VM_PLATFORM_REGISTRY[decl.platform]  # edge validated at finalize
    return capability.shared_backend(decl.platform_config)


def resolve_site(
    name: str,
    registry: Registry,
    *,
    resolver: Resolver | None = None,
) -> VMPlatform:
    """Resolve a site name to its constructed platform instance.

    Returns the platform class instantiated with the site's validated
    ``platform_config`` and the operation's ``resolver`` (construction
    is cheap and never resolves or prompts; the declared config secrets
    register on the resolver for the operation's single resolve pass at
    the preflight boundary). Manager code holds the bound platform and
    never sees ``VM_PLATFORM_REGISTRY`` or platform classes.
    """
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

    decl = lookup_site(name, registry)
    platform_cls = VM_PLATFORM_REGISTRY[decl.platform]  # edge validated at finalize
    return platform_cls(decl.name, decl.platform_config, resolver)


def platform_for(
    vm: VMRow,
    registry: Registry,
    *,
    resolver: Resolver | None = None,
) -> VMPlatform:
    """The bound platform for a VM, resolved through its site."""
    return resolve_site(vm.site, registry, resolver=resolver)


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
        raise ConfigError(
            f"defaults.site names an unknown site '{site}'",
            hint=(
                f"declare a vm-site named '{site}' "
                f"(see `agw resource sample vm-site`) or point defaults.site "
                f"at a declared site (`agw resource list --kind vm-site`)"
            ),
        ) from None
