"""The admin-template resource: the admin user's environment on VMs.

Homed in ``vms/`` by lifecycle, not field shape: the admin user is a
per-VM concept -- provisioned by ``vms/initializer``, exactly one per
VM, and the kind's ``instances()`` iterates VMs. The field set happens
to mirror ``AgentTemplate`` (both describe a user environment), but
ownership follows who provisions and consumes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from agentworks.declared_resource import DeclaredResource
from agentworks.env.entry import EnvTable, env_references
from agentworks.git_credentials.credential import credential_references
from agentworks.schema import ResourceRef

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


class AdminConfig(DeclaredResource):
    """Per-user config for the admin user on VMs.

    Unlike the three inheriting template kinds, every optional field here
    carries a CONCRETE default rather than ``None``: an admin-template is
    not part of a chain, so there is nothing for ``None`` to mean and
    nothing downstream has to supply a fallback (FR15).

    The underlying ``admin-template`` kind was plurified from
    singleton-conceptual to named-multi-instance: ``AdminConfig`` now
    carries its own ``name`` (default ``"default"``) just like the other
    template kinds. Issue #165 adds the ``--admin-template`` CLI flag and
    the VM DB column; that work can land without re-touching the
    framework.
    """

    # Override the base's required ``name``: the admin-template surface is a
    # singleton today, so an omitted-name construction defaults to "default".
    # ``SkipJsonSchema`` rides along because the field is still METADATA:
    # without it the override would re-enter this kind's spec surface.
    name: SkipJsonSchema[str] = "default"

    username: str = "agentworks"
    """The Linux user provisioned as the VM's admin."""

    shell: str = "bash"
    """The admin user's login shell."""

    git_credentials: list[Annotated[str, ResourceRef(kind="git-credential", usage="the git credential")]] = Field(
        default_factory=list
    )
    """Names of ``git-credential`` resources installed for the admin user."""

    user_install_commands: list[
        Annotated[str, ResourceRef(kind="user-install-command", usage="a user install command")]
    ] = Field(default_factory=list)
    """Names of ``user-install-command`` resources run during admin init."""

    dotfiles_source: str | None = None
    """Where to fetch the admin user's dotfiles from. ``None`` installs
    none, which is the only field here with nothing to default to."""

    dotfiles_destination: str = "~/.dotfiles"
    """Where the fetched dotfiles are checked out."""

    dotfiles_install_cmd: str = "./install.sh"
    """The command run inside the checkout to install the dotfiles."""

    mise_activate: bool = True
    """Whether to activate mise in the admin user's shell."""

    mise_packages: list[str] = Field(default_factory=list)
    """Tools to install with mise, each as ``name@version``."""

    mise_lockfile: str | None = None
    """A source reference to a ``mise.lock`` pinning the tool versions."""

    mise_allow_unlocked: bool = False
    """Whether to install ``mise_packages`` with no lockfile present."""

    mise_install_before: str = "7d"
    """How stale an existing mise install may be before it is refreshed:
    a positive duration such as ``7d``, or an ISO date."""

    mise_prune_on_reinit: bool = True
    """Whether re-running init removes mise tools no longer declared."""

    git_force_safe_directory: bool = True
    """Whether to mark checkouts as git ``safe.directory`` for this user."""

    claude_marketplaces: list[str] = Field(default_factory=list)
    """Claude Code marketplaces to register for the admin user."""

    claude_plugins: list[str] = Field(default_factory=list)
    """Claude Code plugins to install for the admin user."""

    env: EnvTable = Field(default_factory=dict)
    """Environment variables exported whenever a shell is opened as the
    admin user."""

    @model_validator(mode="after")
    def _check_mise(self) -> AdminConfig:
        # Imported inside the validator: importing ``agentworks.config``
        # runs the whole config package, and this module is loaded from
        # the kind registry.
        from agentworks.config.validation import check_mise_settings

        check_mise_settings(self.mise_packages, self.mise_lockfile, self.mise_install_before)
        return self

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )

        source = ("admin-template", self.name)
        refs: list[ResourceReference] = list(env_references(self.env, source))
        refs.extend(credential_references(self.git_credentials, source))
        # Install-command references for user_install_commands.
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
