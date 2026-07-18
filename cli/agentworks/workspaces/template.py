"""``WorkspaceTemplate``: the operator-declared workspace-template dataclass.

Moved out of ``agentworks.config`` so the ``workspaces`` domain owns its
declared-resource type next to the resolver
(``agentworks.workspaces.templates``) and the kind
(``agentworks.workspaces.kinds``). ``config.py`` keeps only the legacy
TOML loader that constructs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.env.entry import env_references
from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    from agentworks.env import EnvEntry
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry, ResourceReference


@dataclass(frozen=True)
class WorkspaceTemplate:
    name: str
    inherits: list[str] = field(default_factory=list)
    repo: str | None = None
    tmuxinator: bool | None = None  # None = not explicitly set (inherit/default to True)
    git_user_name: str | None = None  # git user.name for commits in this workspace's repo
    git_user_email: str | None = None  # git user.email for commits in this workspace's repo
    env: dict[str, EnvEntry] = field(default_factory=dict)
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import TemplateReference

        source = ("workspace-template", self.name)
        refs: list[ResourceReference] = list(env_references(self.env, source))
        for parent in self.inherits:
            refs.append(
                TemplateReference(
                    name=parent,
                    kind="workspace-template",
                    usage="a parent template",
                    source=source,
                )
            )
        return refs
