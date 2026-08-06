"""``WorkspaceTemplate``: the operator-declared workspace-template row.

Moved out of ``agentworks.config`` so the ``workspaces`` domain owns its
declared-resource type next to the resolver
(``agentworks.workspaces.templates``) and the kind
(``agentworks.workspaces.kinds``). The ``agentworks.config`` package
keeps only the legacy TOML loader that constructs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from agentworks.declared_resource import DeclaredResource
from agentworks.env import EnvEntry
from agentworks.env.entry import env_references

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


class WorkspaceTemplate(DeclaredResource):
    inherits: list[str] = Field(default_factory=list)
    repo: str | None = None
    tmuxinator: bool | None = None  # None = not explicitly set (inherit/default to True)
    git_user_name: str | None = None  # git user.name for commits in this workspace's repo
    git_user_email: str | None = None  # git user.email for commits in this workspace's repo
    env: dict[str, EnvEntry] = Field(default_factory=dict)

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
