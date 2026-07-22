"""``AgentTemplate``: the operator-declared agent-template dataclass.

Moved out of ``agentworks.config`` so the ``agents`` domain owns its
declared-resource type next to the resolver
(``agentworks.agents.templates``) and the kinds
(``agentworks.agents.kinds``). The agent-shaped ``AdminConfig`` is homed
in ``agentworks.vms.admin`` instead (by lifecycle: the admin user is a
per-VM concept). ``config.py`` keeps only the legacy TOML loader that
constructs this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.declared_resource import DeclaredResource
from agentworks.env.entry import env_references
from agentworks.git_credentials.credential import credential_references

if TYPE_CHECKING:
    from agentworks.env import EnvEntry
    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True, kw_only=True)
class AgentTemplate(DeclaredResource):
    """Agent template definition. All fields are optional (None = inherit/default)."""

    inherits: list[str] = field(default_factory=list)
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

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import (
            TemplateReference,
        )

        source = ("agent-template", self.name)
        refs: list[ResourceReference] = list(env_references(self.env, source))
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
        # Install-command references for user_install_commands.
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
