"""The admin-template resource: the admin user's environment on VMs.

Homed in ``vms/`` by lifecycle, not field shape: the admin user is a
per-VM concept, provisioned by ``vms/initializer`` exactly once per VM.
Database-backed VMs publish their selected admin-template into the
finalized Registry graph. The field set happens
to mirror ``AgentTemplate`` (both describe a user environment), but
ownership follows who provisions and consumes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field, ValidationInfo, model_validator
from pydantic.json_schema import SkipJsonSchema

from agentworks.declared_resource import DeclaredResource
from agentworks.env.entry import EnvTable, env_references
from agentworks.git_credentials.credential import credential_references
from agentworks.schema import ResourceRef

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.inheritance import LayerSource
    from agentworks.resources.reference import ResourceReference
    from agentworks.value_provenance import ProvenancePath


def effective_references(
    effective: AdminConfig,
    source: tuple[str, str],
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
) -> tuple[ResourceReference, ...]:
    """References required by one effective VM admin declaration."""
    from agentworks.resources.reference import ResourceReference as _ResourceReq
    from agentworks.value_provenance import longest_prefix_value

    def owner(path: ProvenancePath) -> tuple[str, str] | None:
        sources = longest_prefix_value(provenance, path) or ()
        return None if not sources else (sources[-1].resource_kind, sources[-1].name)

    by_env = {key: declared_by for key in effective.env if (declared_by := owner(("env", key))) is not None}
    refs: list[ResourceReference] = list(env_references(effective.env, source, by_env))
    by_credential = {
        name: declared_by
        for index, name in enumerate(effective.git_credentials)
        if (declared_by := owner(("git_credentials", index))) is not None
    }
    refs.extend(credential_references(effective.git_credentials, source, by_credential))
    refs.extend(
        _ResourceReq(
            name=name,
            kind="user-install-command",
            usage="a user install command",
            source=source,
            declared_by=owner(("user_install_commands", index)),
        )
        for index, name in enumerate(effective.user_install_commands)
    )
    return tuple(refs)


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
    """What this admin-template is called. Defaults to `default`, which is
    the one `vm create` uses when `--admin-template` names none."""

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
    """Where to fetch the admin user's dotfiles from. Omit to install none."""

    dotfiles_destination: str = "~/.dotfiles"
    """Where the fetched dotfiles are checked out."""

    dotfiles_install_cmd: str = "./install.sh"
    """The command run inside the checkout to install the dotfiles."""

    mise_activate: bool = True
    """Whether to activate mise in the admin user's shell. Write booleans
    unquoted; quoted strings such as ``"no"`` are invalid."""

    mise_packages: list[str] = Field(default_factory=list)
    """Tools to install with mise, each as ``name@version``."""

    mise_lockfile: str | None = None
    """A source reference to a ``mise.lock`` pinning the tool versions."""

    mise_allow_unlocked: bool = False
    """Whether to install ``mise_packages`` with no lockfile present.
    Write booleans unquoted; quoted strings such as ``"no"`` are invalid."""

    mise_install_before: str = "7d"
    """Minimum age for fuzzy mise versions such as ``latest`` or ``node@20``:
    a positive duration such as ``7d``, or an ISO date. Explicitly pinned
    versions install regardless."""

    mise_prune_on_reinit: bool = True
    """Whether re-running init removes mise tools no longer declared.
    Write booleans unquoted; quoted strings such as ``"no"`` are invalid."""

    git_force_safe_directory: bool = True
    """Whether to mark checkouts as git ``safe.directory`` for this user.
    Write booleans unquoted; quoted strings such as ``"no"`` are invalid."""

    claude_marketplaces: list[str] = Field(default_factory=list)
    """Claude Code marketplaces to register for the admin user."""

    claude_plugins: list[str] = Field(default_factory=list)
    """Claude Code plugins to install for the admin user."""

    env: EnvTable = Field(default_factory=dict)
    """Environment variables exported whenever a shell is opened as the
    admin user."""

    @model_validator(mode="after")
    def _check_mise(self, info: ValidationInfo) -> AdminConfig:
        if isinstance(info.context, dict) and info.context.get("partial_declaration") is True:
            return self
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
