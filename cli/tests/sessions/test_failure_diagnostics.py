"""Dead-workload diagnostics cannot replace the observed workload failure."""

from __future__ import annotations

import shlex
import subprocess
import traceback
from uuid import uuid4

import pytest

from agentworks.errors import ConnectivityError, ExternalError, StateError
from agentworks.execution.errors import CommandFailed
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.sessions.tmux import CommandOutcome, _raise_workload_died, access_run_command, capture_output
from tests.vm_access_support import Admin, Call, RecordingVMAccess


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
        return CommandOutcome(
            stdout=output if operation == "alternate" or stage == "cleanup" else "", stderr="", returncode=0
        )

    with pytest.raises(StateError) as caught:
        _raise_workload_died("probe", run_command=run, socket_path="/tmp/unused.sock", exit_status="23")

    assert caught.value.entity_kind == "session" and caught.value.entity_name == "probe"
    assert "23" in str(caught.value)
    assert secondary not in "".join(traceback.format_exception(caught.value))
    assert any("kill-server" in tokens for tokens in calls)
    captures = [tokens for tokens in calls if "capture-pane" in tokens]
    assert all(tokens[tokens.index("-t") + 1] in {"=probe", "=probe:"} for tokens in captures)
    assert (output in str(caught.value)) is (stage != "alternate")


def test_dead_workload_falls_back_after_unsuccessful_main_capture():
    marker = uuid4().hex
    calls: list[list[str]] = []

    def run(command, **kwargs):
        tokens = shlex.split(command)
        calls.append(tokens)
        if "capture-pane" in tokens:
            return (
                CommandOutcome(stdout=marker, stderr="", returncode=0)
                if "-a" in tokens
                else CommandOutcome(stdout="", stderr="", returncode=1)
            )
        return CommandOutcome(stdout="", stderr="", returncode=0)

    with pytest.raises(StateError) as caught:
        _raise_workload_died("probe", run_command=run, socket_path="/tmp/unused.sock", exit_status="23")

    assert marker in str(caught.value) and "23" in str(caught.value)
    assert sum("capture-pane" in tokens for tokens in calls) == 2
    assert any("kill-server" in tokens for tokens in calls)


@pytest.mark.parametrize("stage", ["main", "alternate", "cleanup"])
@pytest.mark.parametrize("failure", ["unknown", "command-failed"])
def test_dead_workload_over_an_access_keeps_status_when_a_request_does_not_complete_or_fails(stage, failure):
    """A dead workload over the admin's access: a capture or kill that does not complete, or fails, is best effort."""
    output = uuid4().hex
    access = RecordingVMAccess([], Admin())

    def answer(call: Call) -> Result | BaseException:
        tokens = shlex.split(call.request.source)
        operation = "cleanup" if "kill-server" in tokens else "alternate" if "-a" in tokens else "main"
        if operation != stage:
            return Result(Completed(0), output.encode() if operation == "alternate" else b"")
        if failure == "unknown":
            return Result(Unknown(UnknownReason.CONNECTION))
        return CommandFailed("box", 1, Result(Completed(1)), streams_logged=True)

    access.answer = answer
    run = access_run_command(access, deadline=None, elevated=False)

    with pytest.raises(StateError) as caught:
        _raise_workload_died("probe", run_command=run, socket_path="/tmp/unused.sock", exit_status="23")

    assert "23" in str(caught.value) and caught.value.entity_name == "probe"
    assert (output in str(caught.value)) is (stage != "alternate")
    assert sum("kill-server" in shlex.split(call.request.source) for call in access.calls) == 1


@pytest.mark.parametrize("stage", ["main", "alternate", "cleanup"])
@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, RuntimeError, StateError])
def test_dead_workload_does_not_suppress_cancellation_or_internal_failure(stage, failure_type):
    failure = failure_type(uuid4().hex)

    def run(command, **kwargs):
        tokens = shlex.split(command)
        operation = "cleanup" if "kill-server" in tokens else "alternate" if "-a" in tokens else "main"
        if operation == stage:
            raise failure
        return CommandOutcome(stdout="", stderr="", returncode=0)

    with pytest.raises(failure_type) as caught:
        _raise_workload_died("probe", run_command=run, socket_path="/tmp/unused.sock", exit_status="23")

    assert caught.value is failure


def test_ordinary_log_capture_remains_strict():
    calls: list[str] = []

    def run(command, **kwargs):
        calls.append(command)
        return CommandOutcome(stdout="", stderr="", returncode=1)

    with pytest.raises(ExternalError):
        capture_output("probe", run_command=run, socket_path="/tmp/unused.sock")

    assert len(calls) == 1
