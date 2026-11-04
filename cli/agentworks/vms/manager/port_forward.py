"""``vm port-forward``: SSH port forwards to a VM, held until the operator stops them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import StateError, ValidationError
from agentworks.interrupts import termination_as_interrupt

from ._helpers import _guard_incomplete_vm, _require_vm
from .boundary import gated_vm_boundary

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.execution.access import VMAccess
    from agentworks.execution.requests import Forward
    from agentworks.secrets.policy import TtyInteractionPolicy


def port_forward_vm(
    db: Database,
    config: Config,
    name: str,
    ports: list[str],
    address: str = "localhost",
    verbose: bool = False,
    *,
    interaction: TtyInteractionPolicy,
) -> int:
    """Forward one or more local ports to a VM via SSH tunnels.

    Returns 0 when the operator stops the forward (Ctrl-C, SIGTERM or SIGHUP) and the
    SSH client's exit status for any other end, including a forward that could
    not be set up. The CLI layer owns the translation to process exit (this
    service function never calls ``sys.exit``). Mirrors ``exec_vm``'s
    return-the-code contract.

    Each port spec is either REMOTE_PORT (local defaults to same) or
    LOCAL_PORT:REMOTE_PORT, matching kubectl port-forward syntax.

    Orchestrated (:func:`gated_vm_boundary`): the graph derives from
    the VM's row, the activation gate replaces this command's
    ``keep_active`` use (opening BEFORE the preflight sweep; its
    just-in-time values seed the boundary resolver), and the
    held-active span covers the foreground SSH tunnel. The port-spec
    validation and the no-Tailscale guard stay pre-gate: a refused
    forward costs zero prompts, zero resolves, and zero gate events.
    """
    import agentworks.execution as execution
    from agentworks.bootstrap import load_request_registry
    from agentworks.execution.access import Admin
    from agentworks.execution.requests import Forward

    # Before any database read or guard: an unsupported client refuses first.
    execution.resolve_ssh()
    vm = _require_vm(db, name)
    _guard_incomplete_vm(vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    forwards = [Forward(local, remote, address) for local, remote in _parse_port_specs(name, ports)]

    # Print forwarding info
    for forward in forwards:
        output.info(f"Forwarding {address}:{forward.local_port} -> {vm.tailscale_host}:{forward.remote_port}")
    if not verbose:
        output.info("Use --verbose for detailed SSH output.")

    access = execution.vm_access(vm, config, Admin())

    # Run in foreground until interrupted
    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(db, config, registry, vm, interaction=interaction):
        return _hold_forward(access, forwards, verbose=verbose)


def _hold_forward(access: VMAccess, forwards: list[Forward], *, verbose: bool) -> int:
    """Run the blocking forward, treating Ctrl-C, ``SIGTERM`` and ``SIGHUP`` as the operator's stop."""
    try:
        with termination_as_interrupt():
            return access.forward(forwards, verbose=verbose)
    except KeyboardInterrupt:
        return 0


def _parse_port_specs(name: str, ports: list[str]) -> list[tuple[int, int]]:
    """``[(local, remote)]`` for each ``[LOCAL:]REMOTE`` spec, or a validation error naming the spec."""
    forwards: list[tuple[int, int]] = []
    for spec in ports:
        parts = spec.split(":")
        if len(parts) == 1:
            try:
                port = int(parts[0])
            except ValueError:
                raise ValidationError(
                    f"invalid port '{spec}'",
                    entity_kind="vm",
                    entity_name=name,
                ) from None
            forwards.append((port, port))
        elif len(parts) == 2:
            try:
                local_port = int(parts[0])
                remote_port = int(parts[1])
            except ValueError:
                raise ValidationError(
                    f"invalid port spec '{spec}'",
                    entity_kind="vm",
                    entity_name=name,
                ) from None
            forwards.append((local_port, remote_port))
        else:
            raise ValidationError(
                f"invalid port spec '{spec}' (expected [LOCAL:]REMOTE)",
                entity_kind="vm",
                entity_name=name,
            )

    for local_port, remote_port in forwards:
        for label, port in [("local", local_port), ("remote", remote_port)]:
            if port < 1 or port > 65535:
                raise ValidationError(
                    f"{label} port {port} out of range (1-65535)",
                    entity_kind="vm",
                    entity_name=name,
                )
    return forwards
