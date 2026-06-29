"""``SessionTemplateKind``: framework strategy for the
``"session_template"`` kind. Same shape as the other template kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.config import SessionTemplate
from agentworks.resources.kind import ALWAYS_MATERIALIZE_SOURCE, KIND_REGISTRY
from agentworks.resources.origin import Origin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.requirement import ResourceRequirement


@dataclass(frozen=True)
class _SessionTemplateKind:
    """Implementation of ``ResourceKind`` for ``"session_template"``."""

    kind: str = "session_template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})

    def synthesize(
        self,
        requirements: Sequence[ResourceRequirement],
    ) -> SessionTemplate:
        """Build the code-defined default ``SessionTemplate``.

        Tolerates ``requirements=()`` per the Phase 2a empty-requirements
        contract; uses the reserved
        ``("framework", "always-materialize")`` source when called that
        way.
        """
        source = requirements[0].source if requirements else ALWAYS_MATERIALIZE_SOURCE
        return SessionTemplate(
            name="default", origin=Origin.auto_declared(source=source)
        )


KIND_REGISTRY["session_template"] = _SessionTemplateKind()
