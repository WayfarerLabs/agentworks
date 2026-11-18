"""Detached work across a lost connection, on the S lane: the VM's sshd stops and starts again.

Vector DT2 shows a launch outlives the connection that started it. This goes
further, with the lane's own sshd: while it is down, observing the launch is
an outcome the observer's loop waits out (``reachability.POLLED``) rather
than an error, and once it is back the next observation reads what the
payload did in the meantime.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import reachability
from agentworks.execution.detached import State
from agentworks.execution.errors import ExecutionIncomplete
from agentworks.execution.requests import Command
from tests.execution.conformance.lanes import SSHDLane, needs_sudo, require_linux_guest, rootless_sshd

if TYPE_CHECKING:
    from pathlib import Path

PAYLOAD_SECONDS = 2
SETTLE_SECONDS = 20


def test_observation_resumes_once_the_vms_sshd_is_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    require_linux_guest()
    directory = tmp_path / "sshd"
    with rootless_sshd(directory) as server:
        lane = SSHDLane(monkeypatch, tmp_path, server)
        needs_sudo(lane)
        detached = lane.access.detached()
        launch = detached.new_launch()
        started = time.monotonic()
        detached.launch(launch, Command(("sh", "-c", f"sleep {PAYLOAD_SECONDS}; echo finished"), elevated=True))
        assert detached.observe(launch).state is State.RUNNING

    with pytest.raises(ExecutionIncomplete) as lost:
        detached.observe(launch)
    assert lost.value.result.completion in reachability.POLLED
    # The payload ends while nothing can reach the VM.
    time.sleep(max(0.0, started + PAYLOAD_SECONDS + 1 - time.monotonic()))

    with rootless_sshd(directory, port=server.port):
        try:
            deadline = time.monotonic() + SETTLE_SECONDS
            while (observed := detached.observe(launch)).state is State.RUNNING:
                assert time.monotonic() < deadline, "the launch did not end"
                time.sleep(0.1)

            assert (observed.state, observed.status, observed.output_tail) == (State.EXITED, 0, b"finished\n")
        finally:
            detached.discard(launch)
