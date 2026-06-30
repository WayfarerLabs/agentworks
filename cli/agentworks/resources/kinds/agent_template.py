"""``AgentTemplateKind``: framework strategy for the ``"agent_template"`` kind.

Same shape as ``VMTemplateKind``: ``auto-declare`` miss policy with
reserved name ``"default"``. ``synthesize`` returns a code-defined
default ``AgentTemplate`` (all optional fields ``None`` per the inherit
shape; the resolver in ``agentworks.agents.templates`` layers concrete
defaults via ``ResolvedAgentTemplate``).

Per-template field-merging stays in ``agentworks.agents.templates``;
the framework owns reference validation and cycle detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.config import AgentTemplate
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
    """Implementation of ``ResourceKind`` for ``"agent_template"``."""

    kind: str = "agent_template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})

    def synthesize(self, references: Sequence[ResourceReference]) -> AgentTemplate:
        """Build the code-defined default ``AgentTemplate``. See
        ``vm_template.py``'s ``synthesize`` for the rationale on why the
        non-empty-``references`` path is preserved even though the
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


KIND_REGISTRY["agent_template"] = _AgentTemplateKind()
