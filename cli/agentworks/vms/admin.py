"""The admin-template resource: the admin user's environment on VMs.

Homed in ``vms/`` by lifecycle, not field shape: the admin user is a
per-VM concept -- provisioned by ``vms/initializer``, exactly one per
VM, and the kind's ``instances()`` iterates VMs. The field set happens
to mirror ``AgentTemplate`` (both describe a user environment), but
ownership follows who provisions and consumes it.
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

    The underlying ``admin-template`` kind was plurified from
    singleton-conceptual to named-multi-instance: ``AdminConfig`` now
    carries its own ``name`` (default ``"default"``) just like the other
    template kinds. The operator-facing surface is unchanged: the loader
    only accepts the ``[admin]`` block and produces
    one instance with name ``"default"``. Issue #165 adds
    ``[admin_templates.<name>]`` parsing, the ``--admin-template`` CLI
    flag, and the VM DB column; that work can land without re-touching
    the framework.
    """

    name: str = "default"
    description: str | None = None
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
        # Catalog references for user_install_commands.
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
