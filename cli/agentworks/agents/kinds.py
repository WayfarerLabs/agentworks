"""``_AgentTemplateKind`` and ``_AdminTemplateKind``: framework strategies
for the ``"agent-template"`` and ``"admin-template"`` kinds.

Both live in the ``agents`` domain package next to the code that
implements agent-shaped templates (``AgentTemplate`` / ``AdminConfig``
in ``agentworks.agents.template``);
``agentworks.resources.kinds.__init__`` imports this module so the kinds
self-register into ``KIND_REGISTRY`` at load.

``AgentTemplateKind`` uses the ``auto-declare`` miss policy with reserved
name ``"default"``. ``synthesize`` returns a code-defined default
``AgentTemplate`` (all optional fields ``None`` per the inherit shape;
the resolver in ``agentworks.agents.templates`` layers concrete defaults
via ``ResolvedAgentTemplate``). Per-template field-merging stays in the
resolver; the framework owns reference validation and cycle detection.

``AdminTemplateKind`` was plurified in Phase 2a.3 from
singleton-conceptual to named-multi-instance, matching the shape of the
other template kinds. The operator-facing surface is unchanged: the
loader still only accepts the singleton ``[admin]`` block and publishes
one ``admin-template:default`` row. A future SDD adds
``[admin_templates.<name>]`` parsing, the ``--admin-template`` CLI flag,
and the VM DB column without re-touching the framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.agents.template import AdminConfig, AgentTemplate
from agentworks.resources.kind import (
    ALWAYS_MATERIALIZE_SOURCE,
    KIND_REGISTRY,
    InstanceRef,
)
from agentworks.resources.origin import Origin

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agentworks.db import Database
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry


@dataclass(frozen=True)
class _AgentTemplateKind:
    """Implementation of ``ResourceKind`` for ``"agent-template"``."""

    kind: str = "agent-template"
    description: str = "Agent user environment templates (shell, dotfiles, mise, ...)"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> AgentTemplate:
        """Build the code-defined default ``AgentTemplate``. See
        ``agentworks.vms.kinds``'s ``synthesize`` for the rationale on why
        the non-empty-``references`` path is preserved even though the
        always-materialize pre-step makes it unreachable today.
        """
        source = references[0].source if references else ALWAYS_MATERIALIZE_SOURCE
        return AgentTemplate(name="default", origin=Origin.auto_declared(source=source))

    def instances(
        self, db: Database, registry: Registry, resource: Any
    ) -> Iterable[InstanceRef]:
        """Every agent whose ``template`` column matches this AgentTemplate's
        name -- or whose ``template`` is NULL when the resource is the
        reserved ``default``.
        """
        name = resource.name
        for agent in db.list_agents():
            if agent.template == name or (
                agent.template is None and name == "default"
            ):
                yield InstanceRef(instance_kind="agent", instance_name=agent.name)


@dataclass(frozen=True)
class _AdminTemplateKind:
    """Implementation of ``ResourceKind`` for ``"admin-template"``."""

    kind: str = "admin-template"
    description: str = "The admin environment template (singleton: default)"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> AdminConfig:
        """Build an empty-defaults ``AdminConfig`` for an auto-declared
        ``admin-template:default``.

        Rarely actually called: ``Config.publish_to`` always publishes a
        real ``admin-template:default`` from ``Config.admin`` (even when
        the operator omits every ``[admin.*]`` section -- the loader
        synthesizes an empty-defaults instance), so the always-materialize
        pre-step's "is the name already in the registry?" short-circuits
        before reaching this method. See ``agentworks.vms.kinds``'s
        ``synthesize`` for the rationale on why the non-empty-
        ``references`` path is preserved.
        """
        source = references[0].source if references else ALWAYS_MATERIALIZE_SOURCE
        return AdminConfig(name="default", origin=Origin.auto_declared(source=source))

    def instances(
        self, db: Database, registry: Registry, resource: Any
    ) -> Iterable[InstanceRef]:
        """Every VM uses the singleton ``admin-template:default`` -- the
        admin template defines the admin user on each VM, and there's one
        admin user per VM. No DB column ties VMs to a non-default admin
        template yet (Phase 2a.3 plurified the framework but the operator
        surface still publishes only ``default``). When/if a future SDD
        adds ``[admin_templates.<name>]`` parsing plus a ``vm.admin-template``
        column, this method changes to filter by that column the same way
        the other template kinds do.
        """
        name = resource.name
        if name != "default":
            return
        for vm in db.list_vms():
            yield InstanceRef(instance_kind="vm", instance_name=vm.name)


KIND_REGISTRY["agent-template"] = _AgentTemplateKind()
KIND_REGISTRY["admin-template"] = _AdminTemplateKind()
