"""Tailscale power-state fast path: reachability probe, rejoin, logout."""

from __future__ import annotations

import contextlib
import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import StateError

from ._helpers import _require_vm

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext, SecretReader
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


def _ensure_tailscale(
    db: Database,
    config: Config,
    vm: VMRow,
    platform: VMPlatform,
    ctx: RunContext,
    *,
    auth_keys: SecretReader,
    auth_key_name: str,
) -> None:
    """Rejoin Tailscale using one explicitly scoped auth-key reader."""
    import agentworks.vms.manager as _mgr
    from agentworks.secrets.line_safety import (
        LineOrientedSecretUse,
        require_line_safe_secret,
    )
    from agentworks.transports import native_transport, transport, wait_for_reconnect

    auth_key = require_line_safe_secret(
        auth_keys.get(auth_key_name),
        use=LineOrientedSecretUse.TAILSCALE,
        secret_name=auth_key_name,
    )
    db.clear_vm_tailscale(vm.name)

    # native_transport() composes Azure's route open/close (heal the
    # public IP, poke and remove the scoped ephemeral SSH allow) via
    # transient_route polymorphism with the reachability probe. Other
    # platforms have a nullcontext transient_route and just build the
    # native transport.
    with contextlib.ExitStack() as _stack:
        # verify_tailscale_available / rejoin_tailscale route through the
        # package object (not a direct import from
        # ``agentworks.vms.initializer``) so tests that monkeypatch
        # ``agentworks.vms.manager.verify_tailscale_available`` /
        # ``.rejoin_tailscale`` (the same two names ``create_vm`` /
        # ``reinit_vm`` call in ``lifecycle.py`` and ``reinit.py``) affect this call site too.
        _mgr.verify_tailscale_available()
        exec_target = native_transport(vm, platform, config, ctx=ctx, stack=_stack)
        # A rejoin command contains the resolved auth key. This fast repair
        # path deliberately has no durable operation log, so reject any future
        # transport change that attaches a redaction-free logger here. A
        # logged rejoin must instead construct its logger with ``auth_key`` in
        # the immutable redaction set before the first write.
        if exec_target.logger is not None:
            raise StateError(
                "Tailscale rejoin transport unexpectedly has an operation logger",
                entity_kind="vm",
                entity_name=vm.name,
            )
        _mgr.rejoin_tailscale(db, vm.name, exec_target, auth_key=auth_key)

    # After the stack unwinds (Azure has removed its transient SSH
    # allow), wait for Tailscale SSH on the new IP to be reachable. The
    # probe is cheap on platforms whose IP didn't change (succeeds on
    # the first try).
    refreshed = db.get_vm(vm.name)
    if refreshed and refreshed.tailscale_host:
        wait_for_reconnect(transport(refreshed, config))

    # Update SSH config in case the Tailscale IP changed
    from agentworks.ssh_config import sync_ssh_config

    sync_ssh_config(config, db)


def _tailscale_rejoin_required(
    db: Database,
    config: Config,
    vm: VMRow,
    *,
    already_running: bool,
) -> bool:
    """Return whether a known Tailscale host requires a rejoin."""
    from agentworks.transports import TailscaleWait, transport, wait_for_reconnect

    refreshed = _require_vm(db, vm.name)
    if not refreshed.tailscale_host:
        return True
    context = TailscaleWait.VERIFY if already_running else TailscaleWait.RECONNECT
    if wait_for_reconnect(transport(refreshed, config), context=context):
        return False
    output.info(f"Tailscale node {refreshed.tailscale_host} did not reconnect, rejoining...")
    return True


def _tailscale_logout(vm: VMRow, config: Config, platform: VMPlatform, ctx: RunContext) -> None:
    """Best-effort: deregister from Tailscale via the provisioning transport.

    Uses ``native_transport(vm, platform, config, ctx=..., stack=...)``
    so the Azure route open/close lifecycle and the reachability probe
    are composed polymorphically; ``ctx`` is the caller's op-start
    context, which that lifecycle's NSG calls read their credential from
    (an SP site authenticates as itself, no ambient fallback). Platforms
    whose factory fails are surfaced through the transport error family,
    which this best-effort cleanup catches and warns.
    """
    from agentworks.transports import native_transport

    output.info("Deregistering from Tailscale...")
    try:
        with contextlib.ExitStack() as stack:
            exec_target = native_transport(vm, platform, config, ctx=ctx, stack=stack)

            # Fire and forget: tailscale down + logout can disrupt
            # networking on the VM, killing SSH-based transports before
            # they get a response. Lima/WSL2 use local transports and
            # are unaffected, but the nohup approach works universally.
            exec_target.run(
                "nohup sh -c 'tailscale down && tailscale logout' >/dev/null 2>&1 &",
                sudo=True,
                timeout=10,
            )
            # Reported here, at the point of the action, so the transcript
            # keeps real order: the stack exit below closes Azure's
            # transient SSH route (its own "Closing SSH route..." line),
            # and nothing after the dispatch confirms the deregistration
            # any further, so printing after the close would misorder the
            # story (#350). Other platforms' transient_route is a
            # nullcontext, so their output is unchanged.
            output.info("Tailscale node deregistered")
    except Exception as e:
        output.warn(f"Tailscale logout failed (node may remain in admin console): {e}")
