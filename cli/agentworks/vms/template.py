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
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import (
        ResourceReference,
        SecretReference,
    )


def tailscale_secret_reference(
    tailscale_auth_key: str,
    template_name: str,
) -> SecretReference:
    """Build the ``SecretReference`` a VMTemplate publishes for its
    Tailscale auth key. Used by both ``VMTemplate.dependencies`` (the
    finalize edge) and ``VMTemplateNode.config_secret_refs`` (the
    preflight sweep's prediction input) so the reference shape is
    single-sourced.
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

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        """This template's outbound edges: its ``inherits`` edges as
        declared, and every runtime need of its EFFECTIVE declaration.

        The two halves read different blobs on purpose (FR17). Inheritance
        is a fact about THIS declaration, so it comes off ``self``; a
        runtime need is a fact about the merged result, so it comes off the
        chain. A child that overrides ``tailscale_auth_key`` therefore
        depends on its override alone, while still inheriting the parent's
        env secrets as edges of its own rather than through a transitive
        walk that could not tell an override from an addition.
        """
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import (
            inherits_reference,
        )
        from agentworks.vms.templates import effective_template

        source = ("vm-template", self.name)
        effective = effective_template({**context.rows_of("vm-template"), self.name: self}, self.name)
        refs: list[ResourceReference] = list(env_references(effective.env, source))
        # Inherits: each parent template name in ``inherits = [...]`` is an
        # INHERITS edge (source composition, not a runtime need; FR17). The
        # framework's VMTemplateKind miss policy auto-declares "default"
        # when missing and errors on any other unknown name; framework
        # cycle detection catches inheritance loops. Per-template
        # field-merging stays in ``agentworks.vms.templates``.
        refs.extend(inherits_reference(parent, source) for parent in self.inherits)
        # Apt / install-command references: each name in apt_packages /
        # system_install_commands resolves to a declared Resource via
        # the framework's miss policy (error on typo, citing this
        # template's source).
        for pkg in effective.apt_packages:
            refs.append(
                _ResourceReq(
                    name=pkg,
                    kind="apt-package",
                    usage="an apt package",
                    source=source,
                )
            )
        for cmd in effective.system_install_commands:
            refs.append(
                _ResourceReq(
                    name=cmd,
                    kind="system-install-command",
                    usage="a system install command",
                    source=source,
                )
            )
        # The effective auth key: the kind's default when nothing in the
        # lineage sets one, the nearest declaration otherwise.
        refs.append(tailscale_secret_reference(effective.tailscale_auth_key, self.name))
        return refs
