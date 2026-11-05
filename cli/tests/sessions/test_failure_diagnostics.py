"""Dead-workload diagnostics cannot replace the observed workload failure."""

from __future__ import annotations

import shlex
import subprocess
import traceback
from uuid import uuid4

import pytest

from agentworks.errors import ConnectivityError, ExternalError, StateError
from agentworks.sessions.tmux import _raise_workload_died, capture_output
from agentworks.ssh import SSHResult


@pytest.mark.parametrize("stage", ["main", "alternate", "cleanup"])
@pytest.mark.parametrize("failure_type", [ExternalError, ConnectivityError, OSError, subprocess.TimeoutExpired])
def test_dead_workload_keeps_status_and_attempts_cleanup_after_external_failure(stage, failure_type):
    secondary = uuid4().hex
    output = uuid4().hex
    failure = failure_type(secondary, 30) if failure_type is subprocess.TimeoutExpired else failure_type(secondary)
    calls: list[list[str]] = []

    def run(command, **kwargs):
        tokens = shlex.split(command)
        calls.append(tokens)
        operation = "cleanup" if "kill-server" in tokens else "alternate" if "-a" in tokens else "main"
        if operation == stage:
            raise failure
        return SSHResult(0, output if operation == "alternate" or stage == "cleanup" else "", "")

    with pytest.raises(StateError) as caught:
        _raise_workload_died("probe", run_command=run, socket_path="/tmp/unused.sock", exit_status="23")

    assert caught.value.entity_kind == "session" and caught.value.entity_name == "probe"
    assert "23" in str(caught.value)
    assert secondary not in "".join(traceback.format_exception(caught.value))
    assert any("kill-server" in tokens for tokens in calls)
    captures = [tokens for tokens in calls if "capture-pane" in tokens]
    assert all(tokens[tokens.index("-t") + 1] == "=probe:" for tokens in captures)
    assert (output in str(caught.value)) is (stage != "alternate")


def test_dead_workload_falls_back_after_unsuccessful_main_capture():
    marker = uuid4().hex
    calls: list[list[str]] = []

    def run(command, **kwargs):
        tokens = shlex.split(command)
        calls.append(tokens)
        if "capture-pane" in tokens:
            return SSHResult(0, marker, "") if "-a" in tokens else SSHResult(1, "", "")
        return SSHResult(0, "", "")

    with pytest.raises(StateError) as caught:
        _raise_workload_died("probe", run_command=run, socket_path="/tmp/unused.sock", exit_status="23")

    assert marker in str(caught.value) and "23" in str(caught.value)
    assert sum("capture-pane" in tokens for tokens in calls) == 2
    assert any("kill-server" in tokens for tokens in calls)


@pytest.mark.parametrize("stage", ["main", "alternate", "cleanup"])
@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, RuntimeError, StateError])
def test_dead_workload_does_not_suppress_cancellation_or_internal_failure(stage, failure_type):
    failure = failure_type(uuid4().hex)

    def run(command, **kwargs):
        tokens = shlex.split(command)
        operation = "cleanup" if "kill-server" in tokens else "alternate" if "-a" in tokens else "main"
        if operation == stage:
            raise failure
        return SSHResult(0, "", "")

    with pytest.raises(failure_type) as caught:
        _raise_workload_died("probe", run_command=run, socket_path="/tmp/unused.sock", exit_status="23")

    assert caught.value is failure


def test_ordinary_log_capture_remains_strict():
    calls: list[str] = []

    def run(command, **kwargs):
        calls.append(command)
        return SSHResult(1, "", "")

    with pytest.raises(ExternalError):
        capture_output("probe", run_command=run, socket_path="/tmp/unused.sock")

    assert len(calls) == 1
