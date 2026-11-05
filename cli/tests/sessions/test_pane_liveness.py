"""Pane liveness requires an explicit native observation, not command success alone."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from agentworks.sessions.tmux import _probe_pane_liveness
from agentworks.ssh import SSHResult


@pytest.mark.parametrize(
    ("observation", "expected"),
    [("0 \n", ("alive", "")), ("1 23\n", ("dead", "23")), ("1 \n", ("dead", ""))],
)
def test_explicit_native_pane_observation_is_accepted(observation, expected):
    calls: list[str] = []

    def run(command, **kwargs):
        calls.append(command)
        return SSHResult(0, observation, "")

    assert _probe_pane_liveness("probe", run_command=run, socket_path="/tmp/unused.sock") == expected
    assert len(calls) == 1


@pytest.mark.parametrize("observation", ["", " \n", "unknown\n", "2 23\n", "0 junk\n", "1 junk\n", "1 23\n0 \n"])
def test_successful_blank_or_malformed_response_retries_without_asserting_alive(observation):
    calls: list[str] = []

    def run(command, **kwargs):
        calls.append(command)
        return SSHResult(0, observation, "")

    assert _probe_pane_liveness("probe", run_command=run, socket_path="/tmp/unused.sock") == ("unverified", "")
    assert len(calls) == 2


@pytest.mark.parametrize("observation", ["0 \n", "1 23\n"])
def test_valid_retry_can_resolve_a_successful_blank_first_response(observation):
    answers = iter([SSHResult(0, "", ""), SSHResult(0, observation, "")])

    def run(command, **kwargs):
        return next(answers)

    expected = ("dead", "23") if observation.startswith("1") else ("alive", "")
    assert _probe_pane_liveness("probe", run_command=run, socket_path="/tmp/unused.sock") == expected
    assert list(answers) == []


def test_nonzero_probe_stays_unverified_without_capture_or_cleanup():
    calls: list[str] = []

    def run(command, **kwargs):
        calls.append(command)
        return SSHResult(1, "1 23\n", "")

    assert _probe_pane_liveness("probe", run_command=run, socket_path="/tmp/unused.sock") == ("unverified", "")
    assert len(calls) == 2
    assert all("display-message" in shlex.split(command) for command in calls)


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, RuntimeError, subprocess.TimeoutExpired])
def test_liveness_probe_does_not_change_exception_behavior(failure_type):
    failure = failure_type("probe", 30) if failure_type is subprocess.TimeoutExpired else failure_type("probe")

    def run(command, **kwargs):
        raise failure

    with pytest.raises(failure_type) as caught:
        _probe_pane_liveness("probe", run_command=run, socket_path="/tmp/unused.sock")
    assert caught.value is failure


@pytest.mark.skipif(os.name != "posix" or shutil.which("tmux") is None, reason="requires a local Unix tmux server")
def test_real_remain_on_exit_pane_is_observed_alive_then_dead():
    executable = shutil.which("tmux")
    assert executable is not None
    # A short private root keeps the Unix socket below sun_path's limit.
    with tempfile.TemporaryDirectory(prefix="agw-pane-", dir="/tmp") as directory:
        root = Path(directory)
        socket = str(root / "tmux.sock")
        gate = root / "exit"
        environment = {
            "HOME": directory,
            "PATH": str(Path(executable).parent) + os.pathsep + os.defpath,
            "SHELL": "/bin/sh",
            "TERM": "xterm",
        }

        def native(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [executable, "-S", socket, *args],
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
            )

        def run(command, **kwargs):
            # Production commands include shell redirects; execute that syntax.
            result = subprocess.run(
                ["/bin/sh", "-c", command], capture_output=True, text=True, timeout=10, env=environment
            )
            return SSHResult(result.returncode, result.stdout, result.stderr)

        try:
            script = f"while [ ! -e {shlex.quote(str(gate))} ]; do sleep 0.02; done; exit 23"
            subprocess.run(
                [executable, "-f", "/dev/null", "-S", socket, "new-session", "-d", "-s", "probe", script],
                capture_output=True,
                check=True,
                timeout=10,
                env=environment,
            )
            native("set-option", "-w", "-t", "=probe:", "remain-on-exit", "on").check_returncode()
            assert _probe_pane_liveness("probe", run_command=run, socket_path=socket) == ("alive", "")
            gate.touch()
            deadline = time.monotonic() + 5
            observed = ""
            while observed != "1 23" and time.monotonic() < deadline:
                result = native("display-message", "-p", "-t", "=probe:", "#{pane_dead} #{pane_dead_status}")
                result.check_returncode()
                observed = result.stdout.strip()
                if observed != "1 23":
                    time.sleep(0.02)
            assert observed == "1 23"
            assert _probe_pane_liveness("probe", run_command=run, socket_path=socket) == ("dead", "23")
        finally:
            native("kill-server")
            assert native("display-message", "-p", "#{pid}").returncode != 0
    assert not root.exists()
