"""``AgentTemplate`` and ``AdminConfig``: the operator-declared agent-shaped
template dataclasses.

Moved out of ``agentworks.config`` so the ``agents`` domain owns its
declared-resource types next to the resolver
(``agentworks.agents.templates``) and the kinds
(``agentworks.agents.kinds``). ``AdminConfig`` lives here because its
field set mirrors ``AgentTemplate`` -- the admin user is agent-shaped.
``config.py`` keeps only the legacy TOML loaders that construct these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.env.entry import env_references
from agentworks.git_credentials.credential import credential_references
from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    from agentworks.env import EnvEntry
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry, ResourceReference


@dataclass(frozen=True)
class AgentTemplate:
    """Agent template definition. All fields are optional (None = inherit/default)."""

    name: str
    inherits: list[str] = field(default_factory=list)
    description: str | None = None
    shell: str | None = None
    git_credentials: list[str] | None = None
    user_install_commands: list[str] | None = None
    dotfiles_source: str | None = None
    dotfiles_destination: str | None = None
    dotfiles_install_cmd: str | None = None
    mise_activate: bool | None = None
    mise_packages: list[str] | None = None
    mise_lockfile: str | None = None
    mise_allow_unlocked: bool | None = None
    mise_install_before: str | None = None
    mise_prune_on_reinit: bool | None = None
    claude_marketplaces: list[str] | None = None
    claude_plugins: list[str] | None = None
    env: dict[str, EnvEntry] = field(default_factory=dict)
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import (
            TemplateReference,
        )

        source = ("agent-template", self.name)
        refs: list[ResourceReference] = list(
            env_references(self.env, source)
        )
        refs.extend(credential_references(self.git_credentials, source))
        for parent in self.inherits:
            refs.append(
                TemplateReference(
                    name=parent,
                    kind="agent-template",
                    usage="a parent template",
                    source=source,
                )
            )
        # Catalog references for user_install_commands.
        for cmd in self.user_install_commands or []:
            refs.append(
                _ResourceReq(
                    name=cmd,
                    kind="user-install-command",
                    usage="a user install command",
                    source=source,
                )
            )
        return refs
