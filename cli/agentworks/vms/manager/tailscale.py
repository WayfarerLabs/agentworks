"""Tailscale around a VM's power state: the reachability fast path, the wait after a start, delete's logout.

This module imports nothing from the legacy transport stack.
"""

from __future__ import annotations

import subprocess
from functools import partial
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ConnectivityError, UserAbort

from ._helpers import _require_vm

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
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


class VMNotAnswering(ConnectivityError):
    """A running VM did not answer over SSH after a start, or has no tailnet address to answer at.

    The hint names ``agw vm recover``, which repairs what a start cannot: a
    removed or expired Tailscale node, a broken sshd, a missing python3.
    """

    def __init__(self, vm_name: str, address: str | None) -> None:
        super().__init__(
            f"VM '{vm_name}' is running but did not answer over SSH at {address}"
            if address
            else f"VM '{vm_name}' is running but has no tailnet address recorded",
            entity_kind="vm",
            entity_name=vm_name,
            hint=f"Run 'agw vm recover {vm_name}'.",
        )


def _await_tailnet(db: Database, config: Config, vm: VMRow, *, already_running: bool) -> None:
    """Wait for a VM that was just started, or was running already, to answer over SSH.

    Re-reads the row for its address and waits with ``reachability.wait`` as
    the admin. A VM that does not answer, or has no address, raises
    :class:`VMNotAnswering`. The wait's own raises (a changed host key, a
    refused key, a VM without python3) propagate with their hints.
    """
    import agentworks.execution as execution
    from agentworks.execution import reachability
    from agentworks.execution.access import Admin

    current = _require_vm(db, vm.name)
    address = current.tailscale_host
    if address:
        wording = reachability.Wording.VERIFY if already_running else reachability.Wording.RECONNECT
        if reachability.wait(execution.vm_access(current, config, Admin()), wording=wording):
            return
    raise VMNotAnswering(vm.name, address)


def _tailscale_logout(vm: VMRow, platform: VMPlatform, ctx: RunContext) -> None:
    """Log the VM's node out of the tailnet over its root channel, best effort, and say what happened.

    Each outcome is reported and none is checked over SSH, since the VM is
    going away. A VM the platform reports stopped or deallocated is not
    asked: no channel reaches it, and a cloud's would wait two minutes to
    find that out (a WSL2 distro the caller's hold has booted reports
    running). A cloud VM whose host key was never recorded refuses inside
    ``run_as_root``, before any route opens: an impostor could report a
    logout that never happened. Either way the node is named for removal
    instead.
    """
    from agentworks.capabilities.vm_platform.tailscale_join import log_out
    from agentworks.db import VMStatus
    from agentworks.execution.errors import TrustMissing

    node = vm.hostname
    removal = f"remove '{node}' in the Tailscale admin console if it is listed there"
    try:
        status = platform.status(vm, ctx)
    except UserAbort:
        raise
    except Exception:
        status = VMStatus.UNKNOWN
    if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        output.warn(f"Tailscale node {node} was not logged out: VM '{vm.name}' is {status.value}; {removal}")
        return
    output.info("Logging out of Tailscale...")
    try:
        result = log_out(partial(platform.run_as_root, vm, ctx))
    except UserAbort:
        raise
    except TrustMissing:
        output.warn(
            f"Tailscale node {node} was not logged out: the host key of VM '{vm.name}' was never "
            f"recorded, so its logout could not be trusted; {removal}"
        )
        return
    except Exception as error:
        output.warn(f"the Tailscale logout of node {node} could not be confirmed ({error}); {removal}")
        return
    if result.status == 0:
        output.info(f"Tailscale node {node} logged out")
    elif result.status is None:
        output.warn(f"the Tailscale logout of node {node} could not be confirmed; {removal}")
    else:
        output.warn(f"'tailscale logout' exited with status {result.status} on node {node}; {removal}")
