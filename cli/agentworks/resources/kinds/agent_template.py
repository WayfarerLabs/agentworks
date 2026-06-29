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
from typing import TYPE_CHECKING, Literal

from agentworks.config import AgentTemplate
from agentworks.resources.kind import ALWAYS_MATERIALIZE_SOURCE, KIND_REGISTRY
from agentworks.resources.origin import Origin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.requirement import ResourceRequirement


@dataclass(frozen=True)
class _AgentTemplateKind:
    """Implementation of ``ResourceKind`` for ``"agent_template"``."""

    kind: str = "agent_template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})

    def synthesize(self, requirements: Sequence[ResourceRequirement]) -> AgentTemplate:
        """Build the code-defined default ``AgentTemplate``.

        Tolerates ``requirements=()`` (the always-materialize pre-step's
        path) per the Phase 2a empty-requirements contract; uses the
        reserved ``("framework", "always-materialize")`` source so the
        breadcrumb is honest about where the row came from.
        """
        source = requirements[0].source if requirements else ALWAYS_MATERIALIZE_SOURCE
        return AgentTemplate(name="default", origin=Origin.auto_declared(source=source))


KIND_REGISTRY["agent_template"] = _AgentTemplateKind()
