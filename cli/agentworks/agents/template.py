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
class AdminConfig:
    """Per-user config for the admin user on VMs.

    Phase 2a.3 plurified the underlying ``admin-template`` kind from
    singleton-conceptual to named-multi-instance: ``AdminConfig`` now
    carries its own ``name`` (default ``"default"``) just like the other
    template kinds. The operator-facing surface is unchanged in this
    phase -- the loader only accepts the ``[admin]`` block and produces
    one instance with name ``"default"``. A future SDD adds
    ``[admin_templates.<name>]`` parsing, the ``--admin-template`` CLI
    flag, and the VM DB column; that work can land without re-touching
    the framework.
    """

    name: str = "default"
    username: str = "agentworks"
    shell: str = "bash"
    git_credentials: list[str] = field(default_factory=list)
    user_install_commands: list[str] = field(default_factory=list)
    dotfiles_source: str | None = None
    dotfiles_destination: str = "~/.dotfiles"
    dotfiles_install_cmd: str = "./install.sh"
    mise_activate: bool = True
    mise_packages: list[str] = field(default_factory=list)
    mise_lockfile: str | None = None
    mise_allow_unlocked: bool = False
    mise_install_before: str = "7d"
    mise_prune_on_reinit: bool = True
    git_force_safe_directory: bool = True
    # Claude Code
    claude_marketplaces: list[str] = field(default_factory=list)
    claude_plugins: list[str] = field(default_factory=list)
    # Env that applies whenever a shell is opened as the admin user.
    env: dict[str, EnvEntry] = field(default_factory=dict)
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )

        source = ("admin-template", self.name)
        refs: list[ResourceReference] = list(
            env_references(self.env, source)
        )
        refs.extend(credential_references(self.git_credentials, source))
        # Catalog references for user_install_commands (Phase 2b).
        for cmd in self.user_install_commands:
            refs.append(
                _ResourceReq(
                    name=cmd,
                    kind="user-install-command",
                    usage="a user install command",
                    source=source,
                )
            )
        return refs


@dataclass(frozen=True)
class AgentTemplate:
    """Agent template definition. All fields are optional (None = inherit/default)."""

    name: str
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
        # Catalog references for user_install_commands (Phase 2b).
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
