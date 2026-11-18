"""Detached work across a lost connection, on the S lane: the VM's sshd stops and starts again.

Vector DT1 shows a launch outlives the connection that started it. This goes
further, with the lane's own sshd: while it is down, observing the launch is
an outcome the observer's loop waits out (``reachability.POLLED``) rather
than an error, and once it is back the next observation reads what the
payload did in the meantime. On S the guest is this machine, so the test
itself lets the payload finish while nothing can reach it.
"""

from __future__ import annotations

import contextlib
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import detached_program, reachability
from agentworks.execution.detached import State
from agentworks.execution.errors import ExecutionIncomplete
from agentworks.execution.requests import Command
from tests.execution.conformance.lanes import SSHDLane, needs_sudo, require_linux_guest, rootless_sshd

if TYPE_CHECKING:
    from pathlib import Path

SETTLE_SECONDS = 20


@pytest.mark.usefixtures("private_detached_base")
def test_observation_resumes_once_the_vms_sshd_is_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    require_linux_guest()
    directory = tmp_path / "sshd"
    with contextlib.ExitStack() as cleanup, contextlib.ExitStack() as first_sshd:
        server = first_sshd.enter_context(rootless_sshd(directory))
        lane = SSHDLane(monkeypatch, tmp_path, server)
        needs_sudo(lane)
        detached = lane.access.detached()
        launch = detached.new_launch()
        go = ("sudo", "-n", "touch", "--", f"{detached_program.BASE}/{launch.id}/work/go")
        # However the test ends, the payload's file is there, so nothing is left waiting for it.
        cleanup.callback(subprocess.run, go, check=False)
        detached.launch(
            launch,
            Command(("sh", "-c", "while [ ! -e go ]; do sleep 0.1; done; echo finished"), elevated=True),
        )
        assert detached.observe(launch).state is State.RUNNING

        first_sshd.close()
        with pytest.raises(ExecutionIncomplete) as lost:
            detached.observe(launch)
        assert lost.value.result.completion in reachability.POLLED
        # The payload finishes while nothing can reach the VM: on S the guest is this machine.
        subprocess.run(go, check=True)

        with rootless_sshd(directory, port=server.port):
            try:
                deadline = time.monotonic() + SETTLE_SECONDS
                while (observed := detached.observe(launch)).state is State.RUNNING:
                    assert time.monotonic() < deadline, "the launch did not end"
                    time.sleep(0.1)

                assert (observed.state, observed.status, observed.output_tail) == (State.EXITED, 0, b"finished\n")
            finally:
                detached.discard(launch)
