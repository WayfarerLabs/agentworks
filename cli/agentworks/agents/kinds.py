"""``_AgentTemplateKind`` and ``_AdminTemplateKind``: framework strategies
for the ``"agent-template"`` and ``"admin-template"`` kinds.

Both live in the ``agents`` domain package next to the code that
implements agent templates (``AgentTemplate`` in
``agentworks.agents.template``);
``agentworks.resources.kinds.__init__`` imports this module so the kind
self-registers into ``KIND_REGISTRY`` at load.

``AgentTemplateKind`` uses the ``auto-declare`` miss policy with reserved
name ``"default"``. ``synthesize`` returns a code-defined default
``AgentTemplate`` (all optional fields ``None`` per the inherit shape;
the resolver in ``agentworks.agents.templates`` layers concrete defaults
via ``ResolvedAgentTemplate``). Per-template field-merging stays in the
resolver; the framework owns reference validation and cycle detection.

``admin-template`` lives in ``agentworks.vms.kinds``: the admin user is
a per-VM concept, provisioned by the VM initializer (issue #165 adds
the per-VM selector).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.agents.template import AgentTemplate
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
    description: str = "Agent user environment templates (shell, tools, dotfiles, mise, ...)"
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


KIND_REGISTRY["agent-template"] = _AgentTemplateKind()
