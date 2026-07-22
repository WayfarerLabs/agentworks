"""Tailscale power-state fast path: reachability probe, rejoin, logout."""

from __future__ import annotations

import contextlib
import signal
import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ConnectivityError, StateError, ValidationError

from ._helpers import _guard_failed_vm, _lookup_or_synthesize_secret, _require_vm
from .boundary import gated_vm_boundary

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMRow

# Guards the missing-tailscale-binary warning to once per process: the
# power-state fast path calls _is_tailscale_reachable on every gated
# command, so an unguarded warn would repeat the same line all run long.
#
# Read and written through the package object (``agentworks.vms.manager``),
# not as a bare module global: tests monkeypatch
# ``agentworks.vms.manager._warned_tailscale_missing`` directly, and a bare
# ``global`` reference here would only ever see THIS module's copy, never
# the package attribute the test patched. The value below is this flag's
# canonical definition (and the package's initial re-exported value); every
# read/write after import goes through ``agentworks.vms.manager``.
_warned_tailscale_missing = False


def _is_tailscale_reachable(tailscale_host: str) -> bool:
    """Quick check whether a Tailscale IP is still reachable.

    Returns False (the degraded answer, which sends the caller down the
    slower cloud-power-state path) on both a ping timeout and a missing
    ``tailscale`` binary. The binary being absent is a setup problem, not
    a transient one: it silently buys a cloud round trip on every gated
    command, so it warns once per process to name the cause rather than
    degrading in silence.
    """
    import agentworks.vms.manager as _mgr

    try:
        result = subprocess.run(
            ["tailscale", "ping", "--timeout=5s", "-c=1", tailscale_host],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        if not _mgr._warned_tailscale_missing:
            _mgr._warned_tailscale_missing = True
            output.warn(
                "tailscale binary not found on PATH; VM power-state checks "
                "will fall back to slower cloud API calls. Install tailscale "
                "(or add it to PATH) to speed them up."
            )
        return False


def port_forward_vm(
    db: Database,
    config: Config,
    name: str,
    ports: list[str],
    address: str = "localhost",
    verbose: bool = False,
) -> int:
    """Forward one or more local ports to a VM via SSH tunnels.

    Returns the underlying SSH process's exit code; the CLI layer owns the
    translation to process exit (this service function never calls
    ``sys.exit``). Mirrors ``exec_vm``'s return-the-code contract.

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
    from agentworks.bootstrap import build_registry

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    # Parse port specs
    forwards: list[tuple[int, int]] = []  # (local_port, remote_port)
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

    # Validate port ranges
    for local_port, remote_port in forwards:
        for label, port in [("local", local_port), ("remote", remote_port)]:
            if port < 1 or port > 65535:
                raise ValidationError(
                    f"{label} port {port} out of range (1-65535)",
                    entity_kind="vm",
                    entity_name=name,
                )

    # Build SSH command with -L flags for each forward
    ssh_cmd = ["ssh", "-N", "-o", "StrictHostKeyChecking=accept-new"]
    if config.operator.ssh_private_key:
        ssh_cmd.extend(["-i", str(config.operator.ssh_private_key)])
    for local_port, remote_port in forwards:
        ssh_cmd.extend(["-L", f"{address}:{local_port}:localhost:{remote_port}"])
    if verbose:
        ssh_cmd.append("-v")
    ssh_cmd.append(f"{vm.admin_username}@{vm.tailscale_host}")

    # Print forwarding info
    for local_port, remote_port in forwards:
        output.info(f"Forwarding {address}:{local_port} -> {vm.tailscale_host}:{remote_port}")
    if not verbose:
        output.info("Use --verbose for detailed SSH output.")

    # Run in foreground until interrupted
    registry = build_registry(config)
    with gated_vm_boundary(db, config, registry, vm):
        try:
            proc = subprocess.Popen(ssh_cmd)

            # Forward SIGINT/SIGTERM to the SSH process for clean shutdown
            def _handle_signal(sig: int, _frame: object) -> None:
                proc.terminate()

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)

            return proc.wait()
        except OSError as e:
            raise ConnectivityError(
                f"failed to start SSH: {e}",
                entity_kind="vm",
                entity_name=name,
            ) from e


def _ensure_tailscale(
    db: Database,
    config: Config,
    vm: VMRow,
    platform: VMPlatform,
    *,
    auth_key_source: Callable[[], str] | None = None,
) -> None:
    """After starting a VM, verify Tailscale connectivity and rejoin if
    needed. ``platform`` is the caller's bound platform (the gates never
    bind, and a re-bind here would re-run the resolve pass).

    ``auth_key_source`` supplies the rejoin auth key when the caller
    owns its resolution: the orchestrated activation gate passes its
    lazy gate-secrets reader (nodes receive, never resolve), so the key
    resolves on this function's first need, with the same
    conditional-need timing as the internal resolve below. ``None``
    keeps today's behavior for the imperative callers: this function
    resolves the key itself, late.
    """
    import agentworks.vms.manager as _mgr
    from agentworks.transports import native_transport, transport, wait_for_reconnect

    # Refresh VM row in case tailscale_host was cleared on stop
    vm = _require_vm(db, vm.name)

    # If we have a known Tailscale host, wait for it to reconnect after boot.
    # This avoids unnecessarily attaching a public IP on Azure.
    if vm.tailscale_host:
        if wait_for_reconnect(transport(vm, config)):
            return

        # Tailscale didn't reconnect (ephemeral key expired, etc.)
        output.info(f"Tailscale node {vm.tailscale_host} did not reconnect, rejoining...")
        db.clear_vm_tailscale(vm.name)

    if auth_key_source is not None:
        auth_key = auth_key_source()
    else:
        # Resolve a fresh Tailscale auth key via the framework before
        # entering the native-transport block; the backend chain handles
        # env-var lookup with prompt fallback. This is the documented
        # conditional-need exception to the resolve-at-the-preflight-boundary
        # contract: whether a rejoin (and therefore a NEW key) is needed is
        # only knowable after starting the VM and watching the node fail to
        # reconnect, so it gets its own late resolve rather than prompting
        # every start for a key that is almost never used.
        from agentworks.bootstrap import build_registry
        from agentworks.secrets import resolve_for_command
        from agentworks.vms.templates import resolve_template

        registry = build_registry(config)
        rejoin_vm_tmpl = resolve_template(registry, vm.template)
        ts_decl = _lookup_or_synthesize_secret(registry, rejoin_vm_tmpl.tailscale_auth_key)
        resolved = resolve_for_command([], config, registry, extra_decls=[ts_decl])
        auth_key = resolved[rejoin_vm_tmpl.tailscale_auth_key]

    # native_transport() composes Azure's attach/detach via
    # transient_route polymorphism with the reachability probe. Other
    # platforms have a nullcontext transient_route and just build the
    # native transport.
    with contextlib.ExitStack() as _stack:
        # verify_tailscale_available / rejoin_tailscale route through the
        # package object (not a direct import from
        # ``agentworks.vms.initializer``) so tests that monkeypatch
        # ``agentworks.vms.manager.verify_tailscale_available`` /
        # ``.rejoin_tailscale`` (the same two names ``create_vm`` /
        # ``reinit_vm`` call in ``lifecycle.py``) affect this call site too.
        _mgr.verify_tailscale_available()
        exec_target = native_transport(vm, platform, config, stack=_stack)
        _mgr.rejoin_tailscale(db, vm.name, exec_target, auth_key=auth_key)

    # After the stack unwinds (Azure detach has fired), wait for
    # Tailscale SSH on the new IP to be reachable. The probe is cheap
    # on platforms whose IP didn't change (succeeds on the first try).
    refreshed = db.get_vm(vm.name)
    if refreshed and refreshed.tailscale_host:
        wait_for_reconnect(transport(refreshed, config))

    # Update SSH config in case the Tailscale IP changed
    from agentworks.ssh_config import sync_ssh_config

    sync_ssh_config(config, db)


def _tailscale_logout(vm: VMRow, config: Config, platform: VMPlatform) -> None:
    """Best-effort: deregister from Tailscale via the provisioning transport.

    Uses ``native_transport(vm, platform, config, stack=...)`` so the
    Azure attach/detach lifecycle and the reachability probe are
    composed polymorphically. Platforms whose factory raises (Proxmox)
    are surfaced as a typed StateError, which we catch and warn.
    """
    from agentworks.transports import native_transport

    output.info("Deregistering from Tailscale...")
    try:
        with contextlib.ExitStack() as stack:
            exec_target = native_transport(vm, platform, config, stack=stack)

            # Fire and forget: tailscale down + logout can disrupt
            # networking on the VM, killing SSH-based transports before
            # they get a response. Lima/WSL2 use local transports and
            # are unaffected, but the nohup approach works universally.
            exec_target.run(
                "nohup sh -c 'tailscale down && tailscale logout' >/dev/null 2>&1 &",
                sudo=True,
                timeout=10,
            )
        output.info("Tailscale node deregistered")
    except Exception as e:
        output.warn(f"Tailscale logout failed (node may remain in admin console): {e}")
