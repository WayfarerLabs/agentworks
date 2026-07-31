"""Composition roots shared by the gate-opening and no-gate VM commands."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks.capabilities.base import RunContext

from ._helpers import _vm_scope

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from agentworks.capabilities.base import OperationScope
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.resources import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import LiveVMNode


@contextlib.contextmanager
def gated_vm_boundary(
    db: Database,
    config: Config,
    registry: Registry,
    vm: VMRow,
    *,
    targets: Sequence[SecretTarget] = (),
    scope: OperationScope | None = None,
) -> Iterator[tuple[LiveVMNode, Resolver]]:
    """The gate-opening commands' shared composition root (vm/agent
    shell and exec, console attach, the workspace lifecycle ops):
    commands that operate interactively on one existing VM. Build the
    live VM node from its row (the site edge holds the bound
    platform), register the walk union AND the command's env-chain
    ``targets`` on the one resolver (site config secrets and runtime
    env secrets are ONE prompt session), then open the ACTIVATION GATE
    before the
    preflight sweep (its just-in-time values seed the boundary
    resolver) and run the one boundary resolve inside it. Yields
    ``(vm_node, resolver)`` within the held-active span: the body's
    interactive or streaming work stays anchored (WSL2's keepalive)
    for the command's duration, and callers read ``resolver.values``
    for env composition.

    ``scope`` is the command's :class:`OperationScope`; when None the
    default VM-level scope for this VM is built. THE RULE: pass the
    level of the entity the command is ABOUT, not of what it walks
    (the graph here is always the live VM alone; the scope names WHY
    the operation runs). The workspace lifecycle callers pass a
    WORKSPACE-level scope, the agent-op callers (agent shell / exec /
    delete / grant / revoke) an AGENT-level one, and the singular
    session ops a SESSION-level one accordingly; the VM default
    serves the commands that are about the VM itself.

    Deliberately NOT :func:`_live_vm_boundary` (the no-gate lifecycle
    trio): these commands converge power state first, and the gate
    ordering (gate, then preflight, then resolve, all inside the span)
    changes the composition's shape rather than adding a flag to it.

    """
    from agentworks.orchestration.activation import (
        activation_gate,
        gate_secret_resolver,
    )
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    resolver = Resolver(config, registry)
    vm_node = live_vm_node(db, config, registry, vm)
    nodes = walk(vm_node)
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    if targets:
        resolver.register_targets(targets)
    if scope is None:
        scope = _vm_scope(db, vm.name)
    with activation_gate(vm_node, gate_secret_resolver(config, registry, resolver)):
        preflight_all(nodes, RunContext(config=config, operation_scope=scope))
        resolver.resolve()
        yield vm_node, resolver


def _live_vm_boundary(
    db: Database,
    config: Config,
    vm: VMRow,
    *,
    registry: Registry | None = None,
) -> tuple[LiveVMNode, RunContext]:
    """The no-gate commands' shared composition root (``start_vm`` /
    ``stop_vm`` / ``delete_vm`` / ``describe_vm``, whose graphs are
    identical): build the live VM node from its row (the site edge
    holds the bound platform), register the walk union on the
    resolver, sweep preflight at VM scope, and run the one boundary
    resolve. Returns the node plus the OP-START context (secrets
    scoped to the site's declared names); callers drive the power ops
    through the held platform (``node.site.platform``) with that
    context, the declare/receive contract's delivery surface.
    ``registry`` reuses a caller-built registry (describe builds one
    early for its degrade-friendly site lookup); ``None`` builds one
    here.

    Deliberately NO activation gate: for start and stop the power op IS
    the command's operation (a command whose op is the state change
    does not converge state first), delete must not gate at all (an
    operator-stopped VM would refuse; broken states are what delete
    exists to clean up), and describe only READS state (a status
    probe is its op; inspecting a stopped VM must never start it).
    """
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import ScopedSecrets, secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    if registry is None:
        registry = build_registry(config)
    resolver = Resolver(config, registry)
    vm_node = live_vm_node(db, config, registry, vm)
    nodes = walk(vm_node)
    for secret_name in secret_union(nodes):
        resolver.register_name(secret_name)
    scope = _vm_scope(db, vm.name)
    preflight_all(nodes, RunContext(config=config, operation_scope=scope))
    resolver.resolve()
    ops_ctx = RunContext(
        config=config,
        operation_scope=scope,
        secrets=ScopedSecrets(resolver.values, vm_node.site.secret_refs()),
    )
    return vm_node, ops_ctx
