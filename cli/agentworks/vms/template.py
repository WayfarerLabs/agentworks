"""``VMTemplate``: the operator-declared VM-template dataclass plus the
Tailscale secret-reference helper.

The ``vms`` domain owns this dataclass (moved out of ``agentworks.config``)
so the declared-resource type lives next to the resolver
(``agentworks.vms.templates``) and the kind (``agentworks.vms.kinds``).
The ``agentworks.config`` package keeps only the legacy TOML loader that
constructs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.declared_resource import DeclaredResource
from agentworks.env.entry import env_references

if TYPE_CHECKING:
    from agentworks.env import EnvEntry
    from agentworks.resources.reference import (
        ResourceReference,
        SecretReference,
    )


def tailscale_secret_reference(
    tailscale_auth_key: str,
    template_name: str,
) -> SecretReference:
    """Build the ``SecretReference`` a VMTemplate publishes for its
    Tailscale auth key. Used by both ``VMTemplate.referenced_resources``
    (raw, in this module) and ``ResolvedVMTemplate.referenced_resources``
    (resolved, in ``agentworks.vms.templates``) so the reference shape
    is single-sourced.
    """
    from agentworks.resources.reference import SecretReference

    return SecretReference(
        name=tailscale_auth_key,
        kind="secret",
        usage="the Tailscale auth key",
        source=("vm-template", template_name),
    )


@dataclass(frozen=True, kw_only=True)
class VMTemplate(DeclaredResource):
    """VM template definition. All optional fields use ``None = inherit``
    semantics except ``tailscale_auth_key``, which is a non-optional
    bare-string secret name (default ``"tailscale-auth-key"``). The
    tailscale field carries no inherit shape because the secret name is a
    deployment-wide convention; operators who want a different name per
    template set it on the specific template.
    """

    inherits: list[str] = field(default_factory=list)
    # Provisioning. Deliberately NO site field: a template describes
    # WHAT a VM is; placement (--site, defaults.site, or the
    # infer/prompt model) is host/operator-scoped, and a shared
    # template must not smuggle a per-host placement decision,
    # especially with bundled sites publishing per-host.
    cpus: int | None = None
    memory: int | None = None
    disk: int | None = None
    swap: int | None = None
    # System-wide initialization
    apt: list[str] | None = None
    apt_packages: list[str] | None = None
    snap: list[str] | None = None
    system_install_commands: list[str] | None = None
    # Env (declared per-template; merged child-overrides-parent at resolution).
    # Plaintext or secret references; the loader produces EnvEntry instances.
    env: dict[str, EnvEntry] = field(default_factory=dict)
    # Secret name for the Tailscale auth key. ``None = inherit`` per the
    # convention used by VMTemplate's other optional fields; the loader
    # sets it to the operator's string when explicit, to ``None`` when
    # omitted. ResolvedVMTemplate (in agentworks.vms.templates) carries
    # the post-inheritance resolved string (default ``"tailscale-auth-key"``).
    # Bare-string only -- no ``{ secret = "..." }`` polymorphism per the
    # SDD; the field IS the secret reference.
    tailscale_auth_key: str | None = None

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import (
            TemplateReference,
        )

        source = ("vm-template", self.name)
        refs: list[ResourceReference] = list(env_references(self.env, source))
        # Inherits: each parent template name in ``inherits = [...]`` is a
        # TemplateReference targeting the same kind. The framework's
        # VMTemplateKind miss policy auto-declares "default" when missing
        # and errors on any other unknown name; framework cycle detection
        # catches inheritance loops. Per-template field-merging stays in
        # ``agentworks.vms.templates``.
        for parent in self.inherits:
            refs.append(
                TemplateReference(
                    name=parent,
                    kind="vm-template",
                    usage="a parent template",
                    source=source,
                )
            )
        # Apt / install-command references: each name in apt_packages /
        # system_install_commands resolves to a declared Resource via
        # the framework's miss policy (error on typo, citing this
        # template's source).
        for pkg in self.apt_packages or []:
            refs.append(
                _ResourceReq(
                    name=pkg,
                    kind="apt-package",
                    usage="an apt package",
                    source=source,
                )
            )
        for cmd in self.system_install_commands or []:
            refs.append(
                _ResourceReq(
                    name=cmd,
                    kind="system-install-command",
                    usage="a system install command",
                    source=source,
                )
            )
        # When the raw template doesn't set tailscale_auth_key, emit the
        # default secret name's reference so the registry finalizes
        # cleanly even before any inheritance walk. ResolvedVMTemplate's
        # referenced_resources emits the inherited value at manager-entry
        # call time.
        ts_name = self.tailscale_auth_key or "tailscale-auth-key"
        refs.append(tailscale_secret_reference(ts_name, self.name))
        return refs
