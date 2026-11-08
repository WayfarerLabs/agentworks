"""Tailscale around a VM's power state: the reachability fast path, and the wait after a start."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ConnectivityError
from agentworks.programs import resolve_program

from ._helpers import _require_vm

if TYPE_CHECKING:
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
            [resolve_program("tailscale"), "ping", "--timeout=5s", "-c=1", tailscale_host],
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
