"""Agent-domain node implementations.

Identity here is INTRINSIC (layer 1): a live agent node knows its VM
from its own row; a pending agent node is constructed with its chosen
name and its edges (its agent-template node and its VM node), so its
identity is complete while it is still pending. Nothing hands identity
down a path, which is what keeps a node reached by several routes
well-defined.

The agent-template node applies the translation rule to the RESOLVED
template: its declared ``git_credentials`` references become dependency
edges to the ``git-credential`` nodes (each of which HOLDS its provider
instance); its env-block secrets are runtime inputs and stay off the
provisioning union, the same hermeticity rule the vm-template node
pins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database
    from agentworks.git_credentials.nodes import GitCredentialNode
    from agentworks.orchestration.node import Node
    from agentworks.resources.registry import Registry
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import LiveVMNode

    from .templates import ResolvedAgentTemplate


class AgentTemplateNode:
    """The resolved ``agent-template`` node: its git-credential
    references are its dependency edges (the generalization of the
    session manager's hand-rolled ephemeral fold: the providers enter
    the plan through the graph, not through the command knowing about
    them). Built by :func:`agent_template_node`."""

    def __init__(
        self,
        tmpl: ResolvedAgentTemplate,
        credentials: tuple[GitCredentialNode, ...],
    ) -> None:
        self._tmpl = tmpl
        self._credentials = credentials

    @property
    def key(self) -> str:
        return f"agent-template/{self._tmpl.name}"

    @property
    def tmpl(self) -> ResolvedAgentTemplate:
        return self._tmpl

    @property
    def credentials(self) -> tuple[GitCredentialNode, ...]:
        """The git-credential nodes this template's references name;
        the realization choreography reads them for the materials
        write."""
        return self._credentials

    def deps(self) -> tuple[Node, ...]:
        return self._credentials

    def secret_refs(self) -> tuple[str, ...]:
        # Env-block secrets are runtime inputs (hermetic provisioning);
        # the token secrets ride the credential nodes' own secret_refs.
        return ()

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...


class LiveAgentNode:
    """An existing agent, from its DB row: intrinsic identity (the row
    carries its VM and linux user), edge to the VM node."""

    def __init__(self, row: AgentRow, vm: LiveVMNode) -> None:
        self._row = row
        self._vm = vm

    @property
    def key(self) -> str:
        return f"agent/{self._row.name}"

    @property
    def name(self) -> str:
        return self._row.name

    @property
    def row(self) -> AgentRow:
        return self._row

    @property
    def linux_user(self) -> str:
        """The agent's Linux user, from its row (intrinsic identity)."""
        return self._row.linux_user

    @property
    def realized(self) -> bool:
        """A live node IS realized: it exists. Present so a consumer
        that watches a target's pending-ness (the required-commands
        check) can hold either a live or a pending agent node."""
        return True

    def deps(self) -> tuple[Node, ...]:
        return (self._vm,)

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...


class PendingAgentNode:
    """The agent a create command will make: name chosen up front,
    edges (agent-template, VM) attached at construction, realized when
    its bespoke mutation (today's ``create_agent`` body) completes.

    ``teardown`` is today's ephemeral-agent rollback body relocated (a
    forced ``delete_agent`` through the VM's bound platform); it runs
    BEFORE a created workspace's teardown in reverse realization order,
    which is what cleans the agent's workspace-group membership up
    before the group itself goes away, exactly today's ordering.
    """

    def __init__(
        self,
        db: Database,
        config: Config,
        name: str,
        template: AgentTemplateNode,
        vm: LiveVMNode,
    ) -> None:
        self._db = db
        self._config = config
        self._name = name
        self._template = template
        self._vm = vm
        self._realized = False

    @property
    def key(self) -> str:
        return f"agent/{self._name}"

    @property
    def name(self) -> str:
        return self._name

    @property
    def linux_user(self) -> str:
        """The realized agent's Linux user, read from its DB row (the
        row is the truth; the realizing mutation derives and stores
        it). Consumers run post-realization, so a missing row is a
        sequencing bug and raises loudly rather than re-deriving a
        value the mutation may not have written yet."""
        row = self._db.get_agent(self._name)
        if row is None:
            raise StateError(
                f"agent '{self._name}' has no DB row yet; linux_user is "
                f"read from the row, only after the realizing mutation "
                f"has inserted it."
            )
        return row.linux_user

    @property
    def template(self) -> AgentTemplateNode:
        return self._template

    def deps(self) -> tuple[Node, ...]:
        return (self._template, self._vm)

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...

    @property
    def realized(self) -> bool:
        return self._realized

    def mark_realized(self) -> None:
        if self._realized:
            raise StateError(
                f"{self.key} was already marked realized; the "
                f"pending-to-realized flip is one-way and once."
            )
        self._realized = True

    def teardown(self) -> None:
        from agentworks.agents.manager import delete_agent

        try:
            delete_agent(
                self._db,
                self._config,
                name=self._name,
                force=True,
                yes=True,
                platform=self._vm.site.platform,
            )
        except Exception as exc:
            # The teardown contract: name the artifact left standing.
            raise StateError(
                f"the ephemeral agent '{self._name}' could not be "
                f"deleted and is left standing: {exc}. Recover with "
                f"'agw agent delete --force {self._name}'.",
                entity_kind="agent",
                entity_name=self._name,
            ) from exc


def agent_template_node(
    registry: Registry, tmpl: ResolvedAgentTemplate, resolver: Resolver | None
) -> AgentTemplateNode:
    """Build the ``agent-template/<name>`` node from the RESOLVED
    template: each name in its declared ``git_credentials`` becomes an
    edge to a ``git-credential`` node (constructed here, one per name,
    each holding its provider instance)."""
    from agentworks.git_credentials.nodes import git_credential_node

    credentials = tuple(
        git_credential_node(registry, cred_name, resolver)
        for cred_name in tmpl.git_credentials
    )
    return AgentTemplateNode(tmpl, credentials)


def live_agent_node(row: AgentRow, vm: LiveVMNode) -> LiveAgentNode:
    """Build the live ``agent/<name>`` node from its row; ``vm`` is the
    node the row's ``vm_name`` points at, shared with every other
    holder."""
    return LiveAgentNode(row, vm)


def pending_agent_node(
    db: Database,
    config: Config,
    name: str,
    template: AgentTemplateNode,
    vm: LiveVMNode,
) -> PendingAgentNode:
    """Build the pending ``agent/<name>`` node with its edges attached.

    The returned object is THE agent node: every holder (the session's
    dep, any readiness that watches the target) must receive this same
    object, so the orchestrator's ``mark_realized`` flip is observed by
    all of them."""
    return PendingAgentNode(db, config, name, template, vm)
