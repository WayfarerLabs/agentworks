"""``VMTemplate``: the operator-declared VM-template row plus the
Tailscale secret-reference helper.

The ``vms`` domain owns this row (moved out of ``agentworks.config``)
so the declared-resource type lives next to the resolver
(``agentworks.vms.templates``) and the kind (``agentworks.vms.kinds``).
The ``agentworks.config`` package keeps only the legacy TOML loader that
constructs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from agentworks.declared_resource import DeclaredResource
from agentworks.env.entry import EnvTable, env_references
from agentworks.schema import ResourceRef, SecretRef
from agentworks.schema.reference import RefRelationship

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import (
        ResourceReference,
        SecretReference,
    )


def tailscale_secret_reference(
    tailscale_auth_key: str,
    template_name: str,
    declared_by: tuple[str, str] | None = None,
) -> SecretReference:
    """Build the ``SecretReference`` a VMTemplate publishes for its
    Tailscale auth key. Used by both ``VMTemplate.dependencies`` (the
    finalize edge) and ``VMTemplateNode.config_secret_refs`` (the
    preflight sweep's prediction input) so the reference shape is
    single-sourced.

    ``declared_by`` names the template in the chain that set the key,
    when that is an ancestor of ``template_name``.
    """
    from agentworks.resources.reference import SecretReference

    return SecretReference(
        name=tailscale_auth_key,
        kind="secret",
        usage="the Tailscale auth key",
        source=("vm-template", template_name),
        declared_by=declared_by,
    )


class VMTemplate(DeclaredResource):
    """VM template definition.

    Every optional field uses ``None = inherit`` semantics, including
    ``tailscale_auth_key``: the merge reads that distinction, so a default
    applied at this layer would make every child override its parent.
    ``ResolvedVMTemplate`` (in ``agentworks.vms.templates``) carries the
    post-inheritance value, and ``"tailscale-auth-key"`` is its default.
    """

    inherits: list[
        Annotated[
            str,
            ResourceRef(
                kind="vm-template",
                usage="a parent template",
                relationship=RefRelationship.INHERITS,
            ),
        ]
    ] = Field(default_factory=list)
    """Parent templates this one composes, nearest last."""

    # Provisioning. Deliberately NO site field: a template describes
    # WHAT a VM is; placement (--site, defaults.site, or the
    # infer/prompt model) is host/operator-scoped, and a shared
    # template must not smuggle a per-host placement decision,
    # especially with bundled sites publishing per-host.
    cpus: int | None = None
    """Virtual CPUs to provision."""

    memory: int | None = None
    """Memory to provision, in GiB."""

    disk: int | None = None
    """Root disk size to provision, in GiB."""

    swap: int | None = None
    """Swap to configure, in GiB."""

    apt: list[str] | None = None
    """Apt packages installed directly, without an ``apt-package`` row."""

    apt_packages: list[Annotated[str, ResourceRef(kind="apt-package", usage="an apt package")]] | None = None
    """Names of ``apt-package`` resources installed during VM init."""

    snap: list[str] | None = None
    """Snap packages installed during VM init."""

    system_install_commands: (
        list[Annotated[str, ResourceRef(kind="system-install-command", usage="a system install command")]] | None
    ) = None
    """Names of ``system-install-command`` resources run during VM init."""

    env: EnvTable = Field(default_factory=dict)
    """Environment variables exported on this VM, as a plaintext value or
    a ``{secret: <name>}`` reference per key. Merged child-overrides-parent
    at resolution."""

    # Bare-string only, no ``{secret: ...}`` polymorphism: the field IS the
    # secret reference. The marker carries NO default_template, and the row
    # base enforces that: this kind composes along an ``inherits`` chain, so
    # filling an absent value would make ``None`` stop meaning "inherit".
    tailscale_auth_key: Annotated[str, SecretRef(usage="the Tailscale auth key")] | None = None
    """The secret naming this VM's Tailscale auth key. Omit it to inherit,
    which falls back to ``tailscale-auth-key`` once the chain resolves."""

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

        Every inherited edge carries the layer that DECLARED it, so an
        error about it names a file that contains the name rather than
        the row that merely publishes the edge.
        """
        from agentworks.resources.inheritance import declarers, merge_layers
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import (
            inherits_reference,
        )
        from agentworks.vms.templates import effective_template

        source = ("vm-template", self.name)
        rows = {**context.rows_of("vm-template"), self.name: self}
        effective = effective_template(rows, self.name)
        layers = merge_layers(rows, self.name)
        by_env = declarers(layers, "vm-template", lambda t: t.env)
        by_pkg = declarers(layers, "vm-template", lambda t: t.apt_packages or ())
        by_cmd = declarers(layers, "vm-template", lambda t: t.system_install_commands or ())
        by_key = declarers(layers, "vm-template", lambda t: (t.tailscale_auth_key,) if t.tailscale_auth_key else ())
        refs: list[ResourceReference] = list(env_references(effective.env, source, by_env))
        # Inherits: each parent template name in ``inherits = [...]`` is an
        # INHERITS edge (source composition, not a runtime need; FR17). The
        # framework's VMTemplateKind miss policy auto-declares "default"
        # when missing and errors on any other unknown name; framework
        # cycle detection catches inheritance loops. Per-template
        # field-merging stays in ``agentworks.vms.templates``.
        refs.extend(inherits_reference(parent, source) for parent in self.inherits)
        # Apt / install-command references: each name in apt_packages /
        # system_install_commands resolves to a declared Resource via
        # the framework's miss policy (error on typo, citing the template
        # that wrote the name).
        for pkg in effective.apt_packages:
            refs.append(
                _ResourceReq(
                    name=pkg,
                    kind="apt-package",
                    usage="an apt package",
                    source=source,
                    declared_by=by_pkg.get(pkg),
                )
            )
        for cmd in effective.system_install_commands:
            refs.append(
                _ResourceReq(
                    name=cmd,
                    kind="system-install-command",
                    usage="a system install command",
                    source=source,
                    declared_by=by_cmd.get(cmd),
                )
            )
        # The effective auth key: the kind's default when nothing in the
        # lineage sets one (nobody declared it, so nobody is named), the
        # nearest declaration otherwise.
        refs.append(
            tailscale_secret_reference(
                effective.tailscale_auth_key,
                self.name,
                by_key.get(effective.tailscale_auth_key),
            )
        )
        return refs
