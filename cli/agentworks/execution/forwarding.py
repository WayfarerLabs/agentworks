"""The blocking port forward.

The library installs no signal handler: the caller owns the operator's
interrupt and decides what a stop means. This module only guarantees that the
client always ends on the way out.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.execution import ssh_process
from agentworks.execution.errors import TrustMismatch

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_POLL_SECONDS = 0.2
_TERMINATE_SECONDS = 1.0


def run_forward(argv: Sequence[str], *, vm_name: str, log: Path | None) -> int:
    """Run the forwarding client until it exits and return its status.

    Never raises for a non-zero status: ``ExitOnForwardFailure`` and
    ``ConnectTimeout`` make a setup failure the same non-zero exit, so there is
    no readiness probe. A client killed by a signal reports 128 plus the
    signal. On a non-zero exit it reports the log's last lines, and a host-key
    failure there is a :class:`TrustMismatch`. With ``log=None`` the client
    inherits the operator's stderr (``verbose``), so there is no tail and no
    trust typing. Polling ``wait`` with a timeout is what lets Ctrl-C arrive.
    """
    process = ssh_process.spawn(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        # An unread stderr pipe stalls the client after about a thousand refused channels.
        stderr=None if log is None else subprocess.DEVNULL,
    )
    try:
        status = ssh_process.exit_status(_wait(process))
    finally:
        _end(process)
    if status != 0 and log is not None:
        text = ssh_process.client_log_text(log)
        tail = ssh_process.tail_lines(text)
        if ssh_process.HOST_KEY_FAILURE in text:
            raise TrustMismatch(vm_name, tail)
        if tail:
            output.warn(f"ssh exited with status {status}:")
            for line in tail.splitlines():
                output.detail(line)
    return status


def _wait(process: subprocess.Popen[bytes]) -> int:
    while True:
        with contextlib.suppress(subprocess.TimeoutExpired):
            return process.wait(timeout=_POLL_SECONDS)


def _end(process: subprocess.Popen[bytes]) -> None:
    """Terminate, wait a second, then kill; a client that already exited is left alone."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_TERMINATE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
