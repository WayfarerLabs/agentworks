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
    from collections.abc import Mapping

    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry, ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl
    from agentworks.vms.base import VMPlatform


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
        from agentworks.vms.platforms import VM_PLATFORM_REGISTRY

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
    template_site: str | None,
    default_site: str | None,
) -> str:
    """SDD R2 selection precedence for `vm create`: the explicit flag,
    then the resolved template's site, then ``defaults.site``, then the
    built-in ``lima`` site.
    """
    return flag or template_site or default_site or "lima"


def lookup_site(name: str, registry: Registry) -> VMSiteDecl:
    """The site's declaration, or the R3 stranded ``ConfigError`` with
    the paste-ready manifest hint (e.g. a migrated remote-Lima row whose
    site manifest the operator has not added yet).
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


def site_secret_decls(decl: VMSiteDecl, registry: Registry) -> list[SecretDecl]:
    """The site's capability-config secret declarations, for the
    consuming command's single resolve pass
    (``compute_needed_secrets(..., extra_decls=...)``).

    The references were derived at finalize (the site emitted them);
    this projects them back to the declared/auto-declared secret rows.
    """
    from agentworks.vms.platforms import VM_PLATFORM_REGISTRY

    capability = VM_PLATFORM_REGISTRY.get(decl.platform)
    if capability is None:
        return []
    decls: list[SecretDecl] = []
    for cref in capability.validate_config(
        f"vm-site/{decl.name}", decl.platform_config
    ):
        if cref.kind == "secret":
            decls.append(registry.lookup("secret", cref.name))
    return decls


def resolve_site(
    name: str,
    registry: Registry,
    *,
    secret_values: Mapping[str, str] | None = None,
) -> VMPlatform:
    """Resolve a site name to its bound platform.

    Returns the platform class instantiated with the site's validated
    ``platform_config`` (and resolved values for any config secrets).
    Manager code holds the bound platform and never sees
    ``VM_PLATFORM_REGISTRY`` or platform classes.
    """
    from agentworks.vms.platforms import VM_PLATFORM_REGISTRY

    decl = lookup_site(name, registry)
    platform_cls = VM_PLATFORM_REGISTRY[decl.platform]  # edge validated at finalize
    return platform_cls(decl.name, decl.platform_config, secret_values)


def platform_for(
    vm: VMRow,
    registry: Registry,
    *,
    secret_values: Mapping[str, str] | None = None,
) -> VMPlatform:
    """The bound platform for a VM, resolved through its site."""
    return resolve_site(vm.site, registry, secret_values=secret_values)


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
