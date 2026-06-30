"""``WorkspaceTemplateKind``: framework strategy for the
``"workspace_template"`` kind. Same shape as the other template kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.config import WorkspaceTemplate
from agentworks.resources.kind import ALWAYS_MATERIALIZE_SOURCE, KIND_REGISTRY
from agentworks.resources.origin import Origin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True)
class _WorkspaceTemplateKind:
    """Implementation of ``ResourceKind`` for ``"workspace_template"``."""

    kind: str = "workspace_template"
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})

    def synthesize(
        self,
        references: Sequence[ResourceReference],
    ) -> WorkspaceTemplate:
        """Build the code-defined default ``WorkspaceTemplate``. See
        ``vm_template.py``'s ``synthesize`` for the rationale on why the
        non-empty-``references`` path is preserved.
        """
        source = references[0].source if references else ALWAYS_MATERIALIZE_SOURCE
        return WorkspaceTemplate(
            name="default", origin=Origin.auto_declared(source=source)
        )


KIND_REGISTRY["workspace_template"] = _WorkspaceTemplateKind()
