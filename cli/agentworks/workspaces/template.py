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
from agentworks.schema import ResourceRef
from agentworks.schema.reference import RefRelationship

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


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
    inherits, and defaults to true when nothing in the chain sets it. A
    boolean, written unquoted: ``false`` and YAML's ``no`` both read as
    false. A QUOTED ``"no"`` is a string, refused now, and it used to
    mean TRUE, the opposite of what it reads as."""

    git_user_name: str | None = None
    """``user.name`` for commits made in this workspace's checkout."""

    git_user_email: str | None = None
    """``user.email`` for commits made in this workspace's checkout."""

    env: EnvTable = Field(default_factory=dict)
    """Environment variables exported in this workspace, as a plaintext
    value or a ``{secret: <name>}`` reference per key."""

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        """The ``inherits`` edges as declared, plus the runtime needs of
        the EFFECTIVE declaration (FR17; see ``VMTemplate.dependencies``
        for the rule the four inheriting kinds share)."""
        from agentworks.resources.inheritance import declarers, merge_layers
        from agentworks.resources.reference import inherits_reference
        from agentworks.workspaces.templates import effective_template

        source = ("workspace-template", self.name)
        rows = {**context.rows_of("workspace-template"), self.name: self}
        effective = effective_template(rows, self.name)
        by_env = declarers(merge_layers(rows, self.name), "workspace-template", lambda t: t.env)
        refs: list[ResourceReference] = list(env_references(effective.env, source, by_env))
        refs.extend(inherits_reference(parent, source) for parent in self.inherits)
        return refs
