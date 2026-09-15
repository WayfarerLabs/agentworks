"""Register artifact bundles as ordinary declarative resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.resources.kind import KIND_REGISTRY, synthesize_no_default
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.declared_resource import DeclaredResource
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.resources.resolved_spec import ResolvedSpec


@dataclass(frozen=True)
class _ArtifactBundleKind:
    kind: str = "artifact-bundle"
    description: str = "Named collections of harness-independent agent artifacts"
    prose: TopicProse = TopicProse(
        title="Artifact bundles",
        overview="""
        An artifact-bundle declares hints, rules, skills, and agents (agent personas)
        in four maps keyed by canonical artifact name. Hints provide small pieces of setup context; rules are always
        loaded into context. Skills use the standard Agent Skills directory format.

        Select bundles through `artifacts.bundles` on VM, admin, agent, workspace, or
        session declarations. An omitted list inherits; a supplied list replaces the
        inherited list, including an empty list. Selections at other scopes remain intact.

        Bundle `inherits` merges each type map by key, replacing an overridden definition
        completely. Within an owner, later selected bundles replace the same type and key.
        Contributions from different owning scopes remain separate, including after deferral.
        Skill and persona frontmatter names must agree with their map keys.

        Hints and rules accept exactly one inline `text` or file `source`. Skills name an
        explicit directory with SKILL.md. Agent personas name a Markdown source with name
        and description frontmatter. Sources use workstation paths or Git references.
        Supporting files can retain exact bytes through relative `preserve_bytes` patterns;
        SKILL.md always uses normalized UTF-8 text.

        The owning scope captures bundle contents before its activated harness integrations
        handle or defer them. A bundle is a declaration, not a live instance. Use
        `agw artifact show` to inspect the selected scope and its actual ancestors.
        """,
    )
    model: type[DeclaredResource] = ArtifactBundle
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "allow"

    def resolve_for_show(self, registry: Registry, name: str) -> ResolvedSpec:
        from agentworks.artifacts.bundle import resolve_bundle
        from agentworks.resources.access import ResourceIdentity
        from agentworks.resources.resolved_spec import project_resolved_spec

        return project_resolved_spec(resolve_bundle(registry, name), ResourceIdentity(self.kind, name))

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        return synthesize_no_default(self.kind, references)


KIND_REGISTRY["artifact-bundle"] = _ArtifactBundleKind()
