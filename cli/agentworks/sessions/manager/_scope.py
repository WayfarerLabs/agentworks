"""Workspace/VM/session resolution and VM activation boundaries."""

from __future__ import annotations

import contextlib
from functools import partial
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks.errors import (
    NotFoundError,
    StateError,
)
from agentworks.vms.manager import gated_vm_boundary

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from agentworks.capabilities.base import OperationScope
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.sessions.tmux import RunCommand
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport
    from agentworks.vms.nodes import LiveVMNode


def _require_workspace(db: Database, name: str) -> WorkspaceRow:
    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )
    return ws


def _require_vm_for_workspace(db: Database, ws: WorkspaceRow) -> VMRow:
    vm = db.get_vm(ws.vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{ws.vm_name}' not found",
            entity_kind="vm",
            entity_name=ws.vm_name,
        )
    return vm


def _session_scope(db: Database, session: SessionRow, ws: WorkspaceRow, vm: VMRow) -> OperationScope:
    """The singular session ops' SESSION-level operation scope: the
    operation is about the session (running as its agent, or as the
    admin), even though the composed graph is the live VM alone; pass
    the level of the entity the command is ABOUT, not of what it
    walks. The SESSION level's field rules (required vm + workspace +
    session; exactly one of agent/admin) are enforced by the scope's
    own constructor."""
    from agentworks.capabilities.base import OperationScope, ScopeLevel
    from agentworks.db import SYSTEM_SLUG_KEY

    return OperationScope(
        level=ScopeLevel.SESSION,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm.name,
        workspace=ws.name,
        session=session.name,
        agent=session.agent_name,
        admin=session.agent_name is None,
    )


@contextlib.contextmanager
def _prepare_vm(
    db: Database,
    config: Config,
    session: SessionRow,
    *,
    operation: str | None = None,
) -> Iterator[tuple[WorkspaceRow, VMRow, RunCommand, RunCommand, Transport]]:
    """The singular session ops' composition root (stop / delete /
    describe / attach / logs): validate the session's workspace and VM
    rows, then yield ``(ws, vm, run_command, run_as_root, target)``
    INSIDE ``gated_vm_boundary``'s held-active span, which replaces
    the imperative bind + point gate and the callers' own ``vm_active``
    holds. The scope is SESSION-level, built here from the session row
    once the vm/workspace rows it names are resolved: the ops are
    about the session, not the VM they walk. If ``operation`` is set,
    an SSHLogger attaches to the Transport so all calls log
    automatically.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHLogger

    ws = _mgr._require_workspace(db, session.workspace_name)
    vm = _require_vm_for_workspace(db, ws)

    # Cheap row validation stays pre-gate: a VM with no Tailscale
    # address can never serve a session op, so it must fail with zero
    # prompts and zero VM starts. (The imperative body checked this
    # after its gate; the gate cannot populate the address on the
    # already-loaded row, so the command's outcome is identical. The
    # hoist does forgo one accidental heal: the post-gate order could
    # start a stopped VM whose rejoin repopulated the row's address,
    # letting a RETRY succeed; now the retry keeps failing until an
    # explicit vm start or reinit.)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{vm.name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=vm.name,
        )

    registry = build_registry(config)
    with gated_vm_boundary(db, config, registry, vm, scope=_session_scope(db, session, ws, vm)):
        logger = SSHLogger(vm.name, operation) if operation else None
        target = _mgr.transport(vm, config, logger=logger)
        run_command: RunCommand = target.run
        run_as_root: RunCommand = partial(target.run, sudo=True)
        yield ws, vm, run_command, run_as_root, target


def _require_session(db: Database, name: str) -> SessionRow:
    session = db.get_session(name)
    if session is None:
        raise NotFoundError(
            f"session '{name}' not found",
            entity_kind="session",
            entity_name=name,
        )
    return session


def _regenerate_tmuxinator(
    db: Database,
    config: Config,
    vm: VMRow,
    ws: WorkspaceRow,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Regenerate the workspace tmuxinator config from current session state."""
    from agentworks.workspaces.tmuxinator import generate_config

    sessions = db.list_sessions(workspace_name=ws.name)
    # Build socket paths for tmuxinator (admin sessions have NULL, agent sessions always set)
    socket_paths = {s.name: s.socket_path for s in sessions}
    config_text = generate_config(ws.name, ws.workspace_path, sessions=sessions, socket_paths=socket_paths)
    target = _mgr.transport(vm, config, logger=logger)
    target.write_file(f"{ws.workspace_path}/.tmuxinator.yml", config_text)


def filter_sessions(
    db: Database,
    *,
    workspace_name: str | list[str] | None = None,
    vm_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    admin_only: bool = False,
) -> list[SessionRow]:
    """Load sessions with optional workspace, VM, agent, and/or admin filters.

    Each name filter accepts a single name or a list of names; lists
    OR within a filter, filters AND across the call. ``admin_only``
    restricts to admin-mode sessions (no agent); it is mutually
    exclusive with ``agent_name`` at the caller level. See
    ``Database.list_sessions``.
    """
    return db.list_sessions(
        workspace_name=workspace_name,
        vm_name=vm_name,
        agent_name=agent_name,
        admin_only=admin_only,
    )


def _distinct_vms_for_sessions(db: Database, sessions: list[SessionRow]) -> list[VMRow]:
    """Resolve the distinct set of VMs that host the given sessions.

    Used by the batch session operations (stop_all_sessions, restart_all_sessions,
    list_sessions) to feed `_batch_vm_boundary` with exactly the VMs whose SSH
    transports will be touched. Order is insertion order keyed by VM name so
    gate and keepalive entry messages render in a stable order.
    """
    distinct: list[VMRow] = []
    seen: set[str] = set()
    for s in sessions:
        ws = db.get_workspace(s.workspace_name)
        if ws is None or ws.vm_name in seen:
            continue
        vm = db.get_vm(ws.vm_name)
        if vm is None:
            continue
        distinct.append(vm)
        seen.add(ws.vm_name)
    return distinct


@contextlib.contextmanager
def _batch_vm_boundary(db: Database, config: Config, vms: Sequence[VMRow]) -> Iterator[None]:
    """The batch session ops' composition root (stop_all_sessions,
    restart_all_sessions, list_sessions' status pass): ONE boundary
    over the distinct VMs, then each VM's activation gate and
    held-active span.

    Mirrors the imperative batch order exactly (preflight + one
    resolve covering the whole batch, THEN per-VM gate + hold),
    orchestrated: one multi-root walk over the live VM nodes, with one
    shared site node per distinct site via the factory's ``site_nodes``
    memo (one held platform instance per site, the old by-site dedup),
    the walk union registered once on ONE resolver, a SYSTEM-level
    scope (one level per COMMAND, never per node; each VM's identity
    comes from its own node), one preflight sweep, ONE boundary
    resolve, and only then the gates in VM order on an ExitStack. A
    failing gate (an operator-stopped VM) propagates and aborts the
    whole batch, exactly as the imperative per-VM gate loop did.

    The gate callback SERVES the boundary's cached values
    (``resolver.get``): the union already covers every gate secret, so
    two stopped VMs sharing a site cost exactly ONE backend pass
    (a per-gate just-in-time resolve would re-resolve the shared
    site's secret, and ``Resolver.seed`` after the boundary raises by
    design). The one exception is the repair path's Tailscale rejoin
    key: it is inherently outside the boundary union (lazy, read only
    when a started VM fails to reconnect) and resolves late through
    the backend chain, the same documented conditional-need exception
    the imperative repair path carried; it cannot seed, because the
    boundary has already resolved. The callback is built PER TARGET
    and refuses any other outside-the-union name unless the target
    declares it in ``repair_secret_refs`` (see the invariant comment
    inline), so no future gate target can silently late-resolve per
    VM.

    An empty VM set stays a complete no-op (no registry, no resolver,
    no gate), the imperative lazy-bind property: ``session list
    --no-status`` and empty filter results must cost nothing here.
    """
    if not vms:
        yield
        return

    from agentworks.bootstrap import build_registry
    from agentworks.capabilities.base import (
        OperationScope,
        RunContext,
        ScopeLevel,
    )
    from agentworks.db import SYSTEM_SLUG_KEY
    from agentworks.orchestration.activation import activation_gate
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import VMSiteNode, live_vm_node

    registry = build_registry(config)
    resolver = Resolver(config, registry)
    site_nodes: dict[str, VMSiteNode] = {}
    vm_nodes = [live_vm_node(db, config, registry, vm, site_nodes=site_nodes) for vm in vms]
    nodes = walk(*vm_nodes)
    union = secret_union(nodes)
    for secret_name in union:
        resolver.register_name(secret_name)
    scope = OperationScope(
        level=ScopeLevel.SYSTEM,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
    )
    preflight_all(nodes, RunContext(config=config, operation_scope=scope))
    resolver.resolve()

    covered = set(union)

    def _gate_resolver(vm_node: LiveVMNode) -> Callable[[str], str]:
        """Per-target gate callback: serve the boundary's cache; guard
        the late-resolve branch against the target's own declaration."""

        def _resolve(secret_name: str) -> str:
            if secret_name in covered:
                return resolver.get(secret_name)
            # INVARIANT: the ONLY sanctioned post-boundary resolution
            # is the repair path's rejoin key. Gate secrets ride the
            # boundary union structurally (gate_secret_refs delegates
            # to the site's declared secret_refs, which the walk
            # unions), so a name outside the covered union must be one
            # of THIS target's declared repair secrets; anything else
            # refuses loudly (the declare/receive contract) rather
            # than silently late-resolving per VM.
            if secret_name not in vm_node.repair_secret_refs():
                raise StateError(
                    f"secret '{secret_name}' is outside the batch "
                    f"boundary union and was not declared in this "
                    f"activation target's repair_secret_refs, so the "
                    f"batch gate will not resolve it late (the "
                    f"declare/receive contract): gate secrets must "
                    f"ride the boundary union; only the lazy rejoin "
                    f"repair key resolves after it."
                )
            # The rejoin repair key: resolve late through the backend
            # chain, never seed (the boundary already resolved).
            from agentworks.orchestration.secrets import secret_declarations
            from agentworks.secrets.resolve import (
                active_backends,
                resolve_secrets,
            )

            (decl,) = secret_declarations([secret_name], registry)
            return resolve_secrets([decl], active_backends(config, registry))[secret_name]

        return _resolve

    with contextlib.ExitStack() as stack:
        for vm_node in vm_nodes:
            stack.enter_context(activation_gate(vm_node, _gate_resolver(vm_node)))
        yield
