"""Workspace-domain node implementations.

Same translation rule as ``vms/nodes.py``: a live node's row fields
become edges (a workspace row's ``vm_name`` is its edge to the VM
node), and a pending node is constructed up front with its chosen name
and its edges attached. Factories take the already-constructed nodes
they depend on, so a graph that reaches the VM from several places
(workspace, agent, session) shares ONE object per node by
construction, the invariant the walk enforces loudly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow
    from agentworks.orchestration.node import Node
    from agentworks.vms.nodes import LiveVMNode


class LiveWorkspaceNode:
    """An existing workspace, from its DB row. An already-created
    workspace has no readiness of its own; its participation is its
    identity and its VM edge."""

    def __init__(self, row: WorkspaceRow, vm: LiveVMNode) -> None:
        self._row = row
        self._vm = vm

    @property
    def key(self) -> str:
        return f"workspace/{self._row.name}"

    @property
    def name(self) -> str:
        return self._row.name

    @property
    def row(self) -> WorkspaceRow:
        return self._row

    def deps(self) -> tuple[Node, ...]:
        return (self._vm,)

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...


class PendingWorkspaceNode:
    """The workspace a create command will make: name chosen up front,
    VM edge attached at construction, realized when its bespoke
    mutation (the ``workspaces.realize.realize_workspace`` body)
    completes.

    ``teardown`` is today's ephemeral-workspace rollback body relocated
    (a forced ``delete_workspace`` through the VM's bound platform).
    ``platform_ctx`` is the orchestrator's op-start-context source for
    that handed-in platform (a callable because teardown runs
    post-boundary, when the resolved values exist; the node itself
    holds no secrets); it rides the INTERIM nested-teardown seam and
    retires with it.
    """

    def __init__(
        self,
        db: Database,
        config: Config,
        name: str,
        vm: LiveVMNode,
        template: str | None,
        platform_ctx: Callable[[], RunContext],
    ) -> None:
        self._db = db
        self._config = config
        self._name = name
        self._vm = vm
        self._template = template
        self._platform_ctx = platform_ctx
        self._realized = False

    @property
    def key(self) -> str:
        return f"workspace/{self._name}"

    @property
    def name(self) -> str:
        return self._name

    @property
    def template(self) -> str | None:
        return self._template

    def deps(self) -> tuple[Node, ...]:
        return (self._vm,)

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
        from agentworks.workspaces.manager import delete_workspace

        try:
            delete_workspace(
                self._db,
                self._config,
                name=self._name,
                force=True,
                yes=True,
                platform=self._vm.site.platform,
                platform_ctx=self._platform_ctx(),
            )
        except Exception as exc:
            # The teardown contract: name the artifact left standing.
            raise StateError(
                f"the ephemeral workspace '{self._name}' could not be "
                f"deleted and is left standing: {exc}. Recover with "
                f"'agw workspace delete --force {self._name}'.",
                entity_kind="workspace",
                entity_name=self._name,
            ) from exc


def live_workspace_node(row: WorkspaceRow, vm: LiveVMNode) -> LiveWorkspaceNode:
    """Build the live ``workspace/<name>`` node from its row; ``vm`` is
    the node the row's ``vm_name`` field points at, constructed once by
    the orchestrator and shared with every other holder."""
    return LiveWorkspaceNode(row, vm)


def pending_workspace_node(
    db: Database,
    config: Config,
    name: str,
    vm: LiveVMNode,
    template: str | None,
    platform_ctx: Callable[[], RunContext],
) -> PendingWorkspaceNode:
    """Build the pending ``workspace/<name>`` node with its VM edge
    attached. ``platform_ctx`` is the teardown's op-start-context
    source (see :class:`PendingWorkspaceNode`)."""
    return PendingWorkspaceNode(db, config, name, vm, template, platform_ctx)
