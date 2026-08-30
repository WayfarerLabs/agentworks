"""``_WorkspaceTemplateKind``: framework strategy for the
``"workspace-template"`` kind. Same shape as the other template kinds.

Lives in the ``workspaces`` domain package next to the code that
implements workspace templates; ``agentworks.resources.kinds.__init__``
imports this module so the kind self-registers into ``KIND_REGISTRY`` at
load.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.origin import Origin
from agentworks.resources.kind import ALWAYS_MATERIALIZE_SOURCE, KIND_REGISTRY
from agentworks.topics import TopicProse
from agentworks.workspaces.template import WorkspaceTemplate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.declared_resource import DeclaredResource
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.resources.resolved_spec import ResolvedSpec


@dataclass(frozen=True)
class _WorkspaceTemplateKind:
    """Implementation of ``ResourceKind`` for ``"workspace-template"``."""

    kind: str = "workspace-template"
    model: type[DeclaredResource] = WorkspaceTemplate
    description: str = "What a workspace clones, and the environment it runs in"
    prose: TopicProse = TopicProse(
        title="Workspace templates",
        overview="""
        A workspace-template says what a workspace IS: which repository it clones, the
        git identity commits are made under, and the environment its sessions run in.
        `agw workspace create --template` selects one, and `default` applies when the
        flag is omitted.

        Repository URLs are HTTPS; authentication comes from the git credentials
        configured on the admin or agent template, never from the URL. Templates compose
        through `inherits`, nearest last, with `env` tables merging key by key.
        """,
    )
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = frozenset({"default"})
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def resolve_for_show(self, registry: Registry, name: str) -> ResolvedSpec:
        """Resolve one concrete workspace template for focused inspection."""
        from agentworks.resources.access import ResourceIdentity
        from agentworks.resources.resolved_spec import project_resolved_spec
        from agentworks.workspaces.templates import resolve_template_with_provenance

        return project_resolved_spec(
            resolve_template_with_provenance(registry, name),
            ResourceIdentity(self.kind, name),
        )

    def synthesize(
        self,
        references: Sequence[ResourceReference],
    ) -> WorkspaceTemplate:
        """Build the code-defined default ``WorkspaceTemplate``. See
        ``agentworks.vms.kinds``'s ``synthesize`` for the rationale on why
        the non-empty-``references`` path is preserved.
        """
        source = references[0].source if references else ALWAYS_MATERIALIZE_SOURCE
        return WorkspaceTemplate(name="default", origin=Origin.auto_declared(source=source))


KIND_REGISTRY["workspace-template"] = _WorkspaceTemplateKind()
