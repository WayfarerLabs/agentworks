"""``WorkspaceTemplate``: the operator-declared workspace-template row,
which is also the ``workspace-template`` kind's spec model.

Homed in the ``workspaces`` domain so the row sits next to the resolver
(``agentworks.workspaces.templates``) and the kind
(``agentworks.workspaces.kinds``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from agentworks.declared_resource import DeclaredResource
from agentworks.env.entry import EnvTable, env_references
from agentworks.schema import CapabilityBlock, MergeStrategy, ResourceRef
from agentworks.schema.reference import RefRelationship

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.inheritance import LayerSource
    from agentworks.resources.reference import ResourceReference
    from agentworks.value_provenance import ProvenancePath
    from agentworks.workspaces.templates import ResolvedTemplate


def effective_references(
    effective: ResolvedTemplate,
    source: tuple[str, str],
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
) -> tuple[ResourceReference, ...]:
    """References required by one effective workspace declaration."""
    from agentworks.capabilities.harness_integration.attachments import attachment_references
    from agentworks.value_provenance import longest_prefix_value

    def owner(key: str) -> tuple[str, str] | None:
        sources = longest_prefix_value(provenance, ("env", key)) or ()
        return None if not sources else (sources[-1].resource_kind, sources[-1].name)

    by_env = {key: declared_by for key in effective.env if (declared_by := owner(key)) is not None}
    refs: list[ResourceReference] = list(env_references(effective.env, source, by_env))
    refs.extend(
        attachment_references(effective.harness_integrations, facet="workspace", source=source, provenance=provenance)
    )
    return tuple(refs)


class WorkspaceTemplate(DeclaredResource):
    """Workspace template definition. Every field is optional, and ``None``
    means "not set here, inherit it" rather than "off"."""

    inherits: list[
        Annotated[
            str,
            ResourceRef(
                kind="workspace-template",
                usage="a parent template",
                relationship=RefRelationship.INHERITS,
            ),
        ]
    ] = Field(default_factory=list)
    """Parent templates this one composes, nearest last."""

    repo: str | None = None
    """The git repository cloned into workspaces built from this template."""

    tmuxinator: bool | None = None
    """Whether to write a tmuxinator project for the workspace. ``None``
    inherits and defaults to true when the chain does not set it. Write
    booleans unquoted; quoted strings such as ``"no"`` are invalid."""

    git_user_name: str | None = None
    """``user.name`` for commits made in this workspace's checkout."""

    git_user_email: str | None = None
    """``user.email`` for commits made in this workspace's checkout."""

    harness_integrations: Annotated[list[CapabilityBlock], MergeStrategy.REPLACE] | None = None
    """Ordered integrations explicitly enabled for native workspace setup.
    An authored list replaces the inherited list; an empty list enables none."""

    env: EnvTable = Field(default_factory=dict)
    """Environment variables exported in this workspace, as a plaintext
    value or a ``{secret: <name>}`` reference per key."""

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        """The ``inherits`` edges as declared, plus the runtime needs of
        the EFFECTIVE declaration (FR17; see ``VMTemplate.dependencies``
        for the rule the four inheriting kinds share)."""
        from agentworks.resources.reference import inherits_reference
        from agentworks.workspaces.templates import effective_template_with_provenance

        source = ("workspace-template", self.name)
        rows = {**context.rows_of("workspace-template"), self.name: self}
        layered = effective_template_with_provenance(rows, self.name)
        refs = list(effective_references(layered.value, source, layered.provenance))
        refs.extend(inherits_reference(parent, source) for parent in self.inherits)
        return refs

    def validate_config(self, context: FinalizeContext) -> None:
        """Validate the effective native setup attachments for this resource."""
        from agentworks.capabilities.harness_integration.attachments import validate_attachments
        from agentworks.workspaces.templates import effective_template_with_provenance

        layered = effective_template_with_provenance(
            {**context.rows_of("workspace-template"), self.name: self}, self.name
        )
        validate_attachments(
            layered.value.harness_integrations,
            facet="workspace",
            source=("workspace-template", self.name),
            provenance=layered.provenance,
            location=self.error_location,
        )
