"""``WorkspaceTemplateKind``: framework strategy for the
``"workspace-template"`` kind. Same shape as the other template kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.config import WorkspaceTemplate
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
class _WorkspaceTemplateKind:
    """Implementation of ``ResourceKind`` for ``"workspace-template"``."""

    kind: str = "workspace-template"
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

    def instances(
        self, db: Database, registry: Registry, resource: Any
    ) -> Iterable[InstanceRef]:
        """Every workspace whose ``template`` column matches this
        WorkspaceTemplate's name -- or whose ``template`` is NULL when
        the resource is the reserved ``default``.
        """
        name = resource.name
        for ws in db.list_workspaces():
            if ws.template == name or (ws.template is None and name == "default"):
                yield InstanceRef(instance_kind="workspace", instance_name=ws.name)


KIND_REGISTRY["workspace-template"] = _WorkspaceTemplateKind()
