"""Proxmox's root channel: the QEMU Guest Agent runs ``/bin/bash -s`` with the script as its input.

QGA always starts commands as root, so there is no elevation. One ``exec``
dispatches the script as ``input-data`` (limited to 65,536 characters), then
``exec-status`` is polled every two seconds until the guest reports an exit
or the deadline passes. QGA has no cancellation endpoint: at the deadline the
result is empty, so the outcome is unknown, and nothing is dispatched again.
No message holds the script, because it can carry a secret.

This module imports nothing from the legacy transport stack.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from agentworks.errors import ConnectivityError
from agentworks.execution.root_script import RawRun

if TYPE_CHECKING:
    from agentworks.plugins.proxmox.api import ProxmoxAPI

INPUT_DATA_LIMIT = 65_536
POLL_INTERVAL_SECONDS = 2.0
_ROOT_SHELL = ["/bin/bash", "-s"]


def run_as_root(api: ProxmoxAPI, node: str, vmid: int, script: bytes, deadline: float) -> RawRun:
    """Run ``script`` through QGA once and return what the guest reported, or empty streams at the deadline.

    A dispatch or status failure, of any kind, raises ``ConnectivityError``
    saying how far the request got, and never chains the original error,
    whose request could quote the script.
    """
    where = f"{vmid}@{node}"
    text = script.decode("utf-8")
    if len(text) > INPUT_DATA_LIMIT:
        raise ConnectivityError(f"a root script for Proxmox VM {where} exceeds QGA's {INPUT_DATA_LIMIT} characters")
    expires = time.monotonic() + deadline
    # Each failure is raised after its handler, so the original is not even the context.
    failure: str | None = None
    try:
        pid = api.guest_agent_exec(node, vmid, command=list(_ROOT_SHELL), input_data=text, timeout=deadline)
    except Exception:
        failure = f"Proxmox QGA dispatch failed for VM {where}; the script may have started"
    if failure is not None:
        raise ConnectivityError(failure)
    while True:
        remaining = expires - time.monotonic()
        if remaining <= 0:
            return RawRun(b"", b"")
        try:
            status = api.guest_agent_exec_status(node, vmid, pid=pid, timeout=remaining)
        except Exception:
            failure = f"Proxmox QGA status failed for VM {where} (PID {pid}); the script may still be running"
        if failure is not None:
            raise ConnectivityError(failure)
        if cast("bool", status["exited"]):
            return RawRun(
                cast("str", status.get("out-data", "")).encode("utf-8"),
                cast("str", status.get("err-data", "")).encode("utf-8"),
            )
        time.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, expires - time.monotonic())))
