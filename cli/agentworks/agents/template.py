"""``AgentTemplate``: the operator-declared agent-template row.

Moved out of ``agentworks.config`` so the ``agents`` domain owns its
declared-resource type next to the resolver
(``agentworks.agents.templates``) and the kinds
(``agentworks.agents.kinds``). The agent-shaped ``AdminConfig`` is homed
in ``agentworks.vms.admin`` instead (by lifecycle: the admin user is a
per-VM concept). The ``agentworks.config`` package keeps only the legacy
TOML loader that constructs this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field, model_validator

from agentworks.declared_resource import DeclaredResource
from agentworks.env.entry import EnvTable, env_references
from agentworks.git_credentials.credential import credential_references
from agentworks.schema import ResourceRef
from agentworks.schema.reference import RefRelationship

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


class AgentTemplate(DeclaredResource):
    """Agent template definition.

    Every field but ``inherits`` and ``env`` is optional and ``None``
    means "not set HERE, inherit it", never "off": the merge reads that
    distinction, so a default applied at this layer would make every child
    override its parent. The concrete values live on the resolved layer.
    """

    inherits: list[
        Annotated[
            str,
            ResourceRef(
                kind="agent-template",
                usage="a parent template",
                relationship=RefRelationship.INHERITS,
            ),
        ]
    ] = Field(default_factory=list)
    """Parent templates this one composes, nearest last."""

    shell: str | None = None
    """The agent user's login shell."""

    git_credentials: list[Annotated[str, ResourceRef(kind="git-credential", usage="the git credential")]] | None = None
    """Names of ``git-credential`` resources installed for the agent user."""

    user_install_commands: (
        list[Annotated[str, ResourceRef(kind="user-install-command", usage="a user install command")]] | None
    ) = None
    """Names of ``user-install-command`` resources run during agent init."""

    dotfiles_source: str | None = None
    """Where to fetch the agent user's dotfiles from."""

    dotfiles_destination: str | None = None
    """Where the fetched dotfiles are checked out."""

    dotfiles_install_cmd: str | None = None
    """The command run inside the checkout to install the dotfiles."""

    mise_activate: bool | None = None
    """Whether to activate mise in the agent user's shell. A boolean, written unquoted:
    ``false`` and YAML's ``no`` both read as false. A QUOTED ``"no"`` is
    a string, refused now, and it used to mean TRUE, the opposite of
    what it reads as."""

    mise_packages: list[str] | None = None
    """Tools to install with mise, each as ``name@version``."""

    mise_lockfile: str | None = None
    """A source reference to a ``mise.lock`` pinning the tool versions."""

    mise_allow_unlocked: bool | None = None
    """Whether to install ``mise_packages`` with no lockfile present.
    A boolean, written unquoted:
    ``false`` and YAML's ``no`` both read as false. A QUOTED ``"no"`` is
    a string, refused now, and it used to mean TRUE, the opposite of
    what it reads as."""

    mise_install_before: str | None = None
    """How OLD a tool version must be before mise will install it, as
    supply-chain defense against a freshly published one: a positive
    duration such as ``7d``, or an ISO date. Only fuzzy requests
    (``latest``, ``node@20``) are filtered; an explicitly pinned version
    installs regardless."""

    mise_prune_on_reinit: bool | None = None
    """Whether re-running init removes mise tools no longer declared.
    A boolean, written unquoted:
    ``false`` and YAML's ``no`` both read as false. A QUOTED ``"no"`` is
    a string, refused now, and it used to mean TRUE, the opposite of
    what it reads as."""

    claude_marketplaces: list[str] | None = None
    """Claude Code marketplaces to register for the agent user."""

    claude_plugins: list[str] | None = None
    """Claude Code plugins to install for the agent user."""

    env: EnvTable = Field(default_factory=dict)
    """Environment variables exported for this agent, as a plaintext value
    or a ``{secret: <name>}`` reference per key."""

    @model_validator(mode="after")
    def _check_mise(self) -> AgentTemplate:
        # Validated against the resolved layer's ``7d`` while STORING
        # ``None``, exactly as the decoder this replaces did: the stored
        # ``None`` is what lets a child inherit its parent's value, and the
        # substituted default is what the check has to run against for an
        # unset field to be legal.
        from agentworks.config.validation import check_mise_settings

        check_mise_settings(self.mise_packages or [], self.mise_lockfile, self.mise_install_before or "7d")
        return self

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        """The ``inherits`` edges as declared, plus the runtime needs of
        the EFFECTIVE declaration (FR17; see ``VMTemplate.dependencies``
        for the rule the four inheriting kinds share)."""
        from agentworks.agents.templates import effective_template
        from agentworks.resources.inheritance import declarers, merge_layers
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import (
            inherits_reference,
        )

        source = ("agent-template", self.name)
        rows = {**context.rows_of("agent-template"), self.name: self}
        effective = effective_template(rows, self.name)
        layers = merge_layers(rows, self.name)
        by_env = declarers(layers, "agent-template", lambda t: t.env)
        by_cred = declarers(layers, "agent-template", lambda t: t.git_credentials or ())
        by_cmd = declarers(layers, "agent-template", lambda t: t.user_install_commands or ())
        refs: list[ResourceReference] = list(env_references(effective.env, source, by_env))
        refs.extend(credential_references(effective.git_credentials, source, by_cred))
        refs.extend(inherits_reference(parent, source) for parent in self.inherits)
        # Install-command references for user_install_commands.
        for cmd in effective.user_install_commands:
            refs.append(
                _ResourceReq(
                    name=cmd,
                    kind="user-install-command",
                    usage="a user install command",
                    source=source,
                    declared_by=by_cmd.get(cmd),
                )
            )
        return refs
