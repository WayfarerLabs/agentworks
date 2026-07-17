"""Throwaway spike for the orchestration-layer SDD (FRD R11).

Implements the `Node` protocol, node adapters over REAL agentworks types
(capability instances, a resolved vm-template, a live VMRow), pending
live nodes, a memoized walker, and the orchestrator-side helpers the
scenarios need. Nothing here is production code and nothing under
`cli/agentworks/` is touched; see README.md and spike-findings.md.

The four bets under test (FRD R11):
1. one thin protocol fits dissimilar nodes without contortion;
2. identity is intrinsic to nodes, injected at construction for leaves;
3. a memoized walk over declared edges reproduces the hand-rolled
   fan-outs (preflight set, secret union);
4. realization-order unwind reproduces today's rollback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agentworks.capabilities.base import Capability, RunContext
from agentworks.errors import StateError
from agentworks.vms.templates import ResolvedVMTemplate, preflight_vm_template

if TYPE_CHECKING:
    from agentworks.db import VMRow
    from agentworks.secrets.resolver import Resolver


# -- The protocol (bet 1) ----------------------------------------------------


@runtime_checkable
class Node(Protocol):
    """The uniform node surface: readiness plus dependency declaration.

    Deliberately THIN (FRD R1): no ops, no teardown, no realized state.
    Ops stay domain-specific; teardown and realization live on the
    creatable-node classes and are the orchestrator's to sequence.
    """

    @property
    def key(self) -> str: ...

    def deps(self) -> tuple[Node, ...]: ...

    def secret_refs(self) -> tuple[str, ...]: ...

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...


class SpikeError(StateError):
    """Loud-error type for the anti-silent-skip assertions."""


# -- Node adapters over real types -------------------------------------------


@dataclass
class CapabilityInstanceNode:
    """Adapter: any of today's `Capability` instances is a node.

    The lifecycle already lives here (the FRD's observation 1), so the
    adapter is nearly free: key from the instance's owner identity,
    secret refs from re-running the pure `validate_config`, readiness
    delegated. This is the one node species that fits with zero new
    code of its own.
    """

    instance: Capability

    @property
    def key(self) -> str:
        return f"{type(self.instance).owner_kind}/{self.instance.owner_name}"

    def deps(self) -> tuple[Node, ...]:
        return ()

    def secret_refs(self) -> tuple[str, ...]:
        refs = type(self.instance).validate_config(self.key, self.instance.config)
        return tuple(r.name for r in refs if r.kind == "secret")

    def preflight(self, ctx: RunContext) -> None:
        self.instance.preflight(ctx)

    def runup(self, ctx: RunContext) -> None:
        # Not exercised in the spike: the platform/provider runups probe
        # real endpoints, and runup timing is proven on the harness stub.
        pass


@dataclass
class VMTemplateNode:
    """Adapter: the resolved vm-template, absorbing the free function
    `preflight_vm_template` as its readiness (FRD R1's relocation).

    Holds a resolver because today's function needs one for prediction;
    under R5 that prediction moves central and this seam disappears.
    Recorded as a finding, not fixed here.
    """

    tmpl: ResolvedVMTemplate
    resolver: Resolver

    @property
    def key(self) -> str:
        return f"vm-template/{self.tmpl.name}"

    def deps(self) -> tuple[Node, ...]:
        return ()

    def secret_refs(self) -> tuple[str, ...]:
        return tuple(
            r.name for r in self.tmpl.referenced_resources() if r.kind == "secret"
        )

    def preflight(self, ctx: RunContext) -> None:
        preflight_vm_template(self.tmpl, self.resolver)

    def runup(self, ctx: RunContext) -> None:
        pass


@dataclass
class LiveVMNode:
    """A live resource: an existing VM, constructed from its real DB row.

    Identity is intrinsic (the row carries its own names); the platform
    instance is a dependency EDGE, not a contained field (FRD R3).
    """

    row: VMRow
    platform: CapabilityInstanceNode
    realized: bool = True

    @property
    def key(self) -> str:
        return f"vm/{self.row.name}"

    def deps(self) -> tuple[Node, ...]:
        return (self.platform,)

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None:
        pass  # an already-running VM has nothing to check here

    def runup(self, ctx: RunContext) -> None:
        pass


@dataclass
class PendingNode:
    """Base machinery for live resources the command will create.

    Pending-ness is a property of the node (FRD R3), queryable by any
    readiness check that needs the target; `to_create` does not exist.
    The orchestrator flips `realized` when the mutation completes and
    records the order for unwind.
    """

    name: str
    kind: str = field(init=False, default="pending")
    _deps: tuple[Node, ...] = ()
    realized: bool = False
    torn_down: bool = False

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.name}"

    def deps(self) -> tuple[Node, ...]:
        return self._deps

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None:
        pass

    def runup(self, ctx: RunContext) -> None:
        pass

    def realize(self) -> None:
        self.realized = True

    def teardown(self) -> None:
        """The node-scope half of unwind (FRD R1/R4): delete what the
        realizing mutation made. In production this is today's rollback
        body (delete the DB row, delete the user); here it records."""
        self.torn_down = True


@dataclass
class PendingVMNode(PendingNode):
    kind: str = field(init=False, default="vm")


@dataclass
class PendingWorkspaceNode(PendingNode):
    kind: str = field(init=False, default="workspace")


@dataclass
class PendingAgentNode(PendingNode):
    """Identity intrinsic to the node (FRD R3): the pending agent knows
    its own chain of names from construction, nothing threads a
    separate identity object."""

    kind: str = field(init=False, default="agent")
    vm_name: str = ""
    workspace_name: str = ""


@dataclass
class AgentTemplateNode:
    """The agent template: its git-credential readiness IS its provider
    dependencies. This edge is what generalizes `create_session`'s
    hand-rolled ephemeral fold (`_preflight_resolve_agent_git`): the
    providers enter the plan through the graph, not through the command
    knowing about them (FRD R4)."""

    name: str
    providers: tuple[CapabilityInstanceNode, ...]

    @property
    def key(self) -> str:
        return f"agent-template/{self.name}"

    def deps(self) -> tuple[Node, ...]:
        return self.providers

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None:
        pass

    def runup(self, ctx: RunContext) -> None:
        pass


@dataclass
class ProbeRecord:
    """What the harness probe knew when it fired: proves the injected
    identity (session name) and the target's intrinsic identity both
    reach a leaf capability node."""

    session_name: str
    agent_name: str
    vm_name: str
    workspace_name: str
    commands: tuple[str, ...]


@dataclass
class HarnessStubNode:
    """Leaf capability node standing in for the harness (FRD R11).

    Identity by INJECTION (FRD R3): constructed WITH the session name it
    addresses, supplied by the orchestrator; it never walks the graph to
    find it. Its required-commands check floats between preflight and
    runup by the target's pending-ness, replacing the harness SDD's
    `to_create` signal, and fires exactly once.
    """

    session_name: str
    target: PendingAgentNode | None
    required_commands: tuple[str, ...]
    probes: list[ProbeRecord] = field(default_factory=list)
    deferred: bool = False

    @property
    def key(self) -> str:
        return f"harness/{self.session_name}"

    def deps(self) -> tuple[Node, ...]:
        return (self.target,) if self.target is not None else ()

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None:
        self._check()

    def runup(self, ctx: RunContext) -> None:
        self._check()

    def _check(self) -> None:
        # Anti-silent-skip (FRD R3): a MISSING target is a loud error;
        # only an explicitly pending one defers.
        if self.target is None:
            raise SpikeError(
                f"harness '{self.session_name}': no target agent node; "
                f"refusing to skip the required-commands check"
            )
        if self.probes:
            return  # fired-once falls out of the probe being recorded
        if not self.target.realized:
            self.deferred = True
            return
        self.probes.append(
            ProbeRecord(
                session_name=self.session_name,
                agent_name=self.target.name,
                vm_name=self.target.vm_name,
                workspace_name=self.target.workspace_name,
                commands=self.required_commands,
            )
        )


# -- The orchestrator-side helpers (FRD R4; shared-toolkit candidates) --------


def walk(*roots: Node) -> list[Node]:
    """Memoized post-order walk over declared edges: dependencies before
    dependents, each node once (by key), cycles are errors."""
    order: list[Node] = []
    done: set[str] = set()
    visiting: list[str] = []

    def visit(node: Node) -> None:
        if node.key in done:
            return
        if node.key in visiting:
            chain = " -> ".join((*visiting, node.key))
            raise SpikeError(f"dependency cycle: {chain}")
        visiting.append(node.key)
        for dep in node.deps():
            visit(dep)
        visiting.pop()
        done.add(node.key)
        order.append(node)

    for root in roots:
        visit(root)
    return order


def preflight_all(nodes: list[Node], ctx: RunContext) -> None:
    """The preflight-all sweep: every participating node, before any
    prompt or mutation (the walk-away invariant's first half)."""
    for node in nodes:
        node.preflight(ctx)


def secret_union(nodes: list[Node]) -> set[str]:
    """The union the single resolve pass must cover, computed from
    declarations over the plan (FRD R5): central, no instance registers
    itself anywhere."""
    return {name for node in nodes for name in node.secret_refs()}


@dataclass
class RealizationLog:
    """The minimal shared state unwind needs (the first-consumer review's
    note 1): what has been realized, in order. Vocabulary-not-artifact
    holds: this is a list, not a Plan class."""

    realized: list[PendingNode] = field(default_factory=list)

    def realize(self, node: PendingNode) -> None:
        node.realize()
        self.realized.append(node)

    def unwind(self) -> list[str]:
        """Tear down realized nodes in reverse realization order, the
        orchestrator-owned half of rollback (FRD R4)."""
        torn: list[str] = []
        for node in reversed(self.realized):
            node.teardown()
            torn.append(node.key)
        self.realized.clear()
        return torn
