"""Focused checks for the private workload shell observation boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from agentworks.execution import _workload_shell_guest as guest
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState, RuntimeSelection, RuntimeTargetOS
from agentworks.execution._workload_shell import (
    WorkloadShellObservationError,
    WorkloadShellObservationState,
    _BoundedResponseSink,
    observe_workload_shell,
)
from agentworks.execution._workload_shell_bundle import FIXED_SOURCE
from agentworks.execution._workload_shell_protocol import (
    MAX_WORKLOAD_SHELL_REQUEST_BYTES,
    MAX_WORKLOAD_SHELL_RESPONSE_BYTES,
    WorkloadShellFailure,
    WorkloadShellRequest,
    WorkloadShellResponse,
    WorkloadShellWireError,
    decode_workload_shell_request,
    decode_workload_shell_response,
    encode_workload_shell_request,
    encode_workload_shell_response,
)
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)

NONCE = "a" * 32


def _plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


def _selection() -> RuntimeSelection:
    return RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)


@dataclass
class CarrierStub:
    response: bytes
    prefix_state: str = "ready"
    stdout_complete: bool = True
    stderr_complete: bool = True
    stderr: bytes = b""
    failure: Failure | None = None
    calls: int = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        pass

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        assert isinstance(io.input, FiniteInput)
        assert isinstance(io.output, SinkOutput)
        nonce = invocation.argv[invocation.argv.index("agentworks-runtime-prerequisite") + 1]
        payload = self.response.replace(NONCE.encode(), nonce.encode())
        io.output.stdout.try_write(memoryview(f"AGW_RUNTIME_1:{nonce}:{self.prefix_state}:0\n".encode() + payload))
        io.output.stderr.try_write(memoryview(self.stderr))
        return CarrierReport(
            Dispatch.SENT,
            ExitStatus(code=0),
            0,
            CapturedOutput(complete=self.stdout_complete, retention=Retention.DELIVERED),
            CapturedOutput(complete=self.stderr_complete, retention=Retention.DELIVERED),
            self.failure,
        )


def _observe(carrier: CarrierStub):
    return observe_workload_shell(carrier, plan=_plan(), runtime_selection=_selection(), deadline=Deadline.after(5))


@pytest.mark.parametrize("shell", ["/bin/sh", "/usr/bin/sh", "/bin/bash", "/usr/bin/bash"])
def test_host_accepts_fixed_supported_shell(shell: str) -> None:
    carrier = CarrierStub(encode_workload_shell_response(NONCE, WorkloadShellResponse(shell=shell)))
    result = _observe(carrier)
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.observation is not None
    assert result.observation.state is WorkloadShellObservationState.RESOLVED
    assert result.observation.shell == shell
    assert carrier.calls == 1


def test_guest_refuses_unsupported_and_missing_shell() -> None:
    with patch("pwd.getpwuid") as lookup:
        lookup.return_value.pw_shell = "/bin/zsh"
        assert guest._shell().failure is WorkloadShellFailure.UNSUPPORTED
        lookup.return_value.pw_shell = ""
        assert guest._shell().failure is WorkloadShellFailure.MISSING


def test_guest_reads_current_effective_user_shell() -> None:
    with patch("pwd.getpwuid") as lookup:
        lookup.return_value.pw_shell = "/bin/sh"
        with patch.object(os, "stat") as stat_path, patch.object(os, "access", return_value=True):
            stat_path.return_value.st_mode = 0o100755
            assert guest._shell().shell == "/bin/sh"
        lookup.assert_called_once_with(os.geteuid())


def test_guest_refuses_non_executable_or_missing_object() -> None:
    with patch("pwd.getpwuid") as lookup:
        lookup.return_value.pw_shell = "/bin/sh"
        with patch.object(os, "stat") as stat_path, patch.object(os, "access", return_value=False):
            stat_path.return_value.st_mode = 0o100644
            assert guest._shell().failure is WorkloadShellFailure.UNSUPPORTED
        with patch.object(os, "stat", side_effect=FileNotFoundError):
            assert guest._shell().failure is WorkloadShellFailure.MISSING


def test_guest_identity_mismatch_refuses_before_lookup() -> None:
    wrong = IdentityExpectation(os.geteuid() + 1, os.getegid(), _plan().expected.groups)
    request = encode_workload_shell_request(WorkloadShellRequest(NONCE, wrong))
    with (
        patch.object(guest, "_read_request", return_value=request),
        patch.object(guest, "_write") as write,
        patch.object(guest, "_shell") as lookup,
    ):
        guest.main(NONCE)
    lookup.assert_not_called()
    assert decode_workload_shell_response(write.call_args.args[0], NONCE).failure is WorkloadShellFailure.IDENTITY


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"{", WorkloadShellObservationError.RESPONSE),
        (b"x" * (MAX_WORKLOAD_SHELL_RESPONSE_BYTES + 1), WorkloadShellObservationError.OVERSIZED),
        (b"", WorkloadShellObservationError.MISSING),
    ],
)
def test_invalid_or_incomplete_response(payload: bytes, error: WorkloadShellObservationError) -> None:
    result = _observe(CarrierStub(payload))
    assert result.observation is not None
    assert result.observation.error is error


def test_incomplete_stream_and_carrier_failure_remain_separate() -> None:
    payload = encode_workload_shell_response(NONCE, WorkloadShellResponse(shell="/bin/sh"))
    result = _observe(CarrierStub(payload, stdout_complete=False, failure=Failure.DEADLINE))
    assert result.carrier_failure is Failure.DEADLINE
    assert result.observation is not None
    assert result.observation.state is WorkloadShellObservationState.INCOMPLETE


def test_runtime_uncertainty_withholds_helper_observation() -> None:
    result = _observe(CarrierStub(b"", prefix_state="unusable"))
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.UNUSABLE
    assert result.observation is None


def test_expired_deadline_does_not_dispatch() -> None:
    carrier = CarrierStub(b"")
    result = observe_workload_shell(carrier, plan=_plan(), runtime_selection=_selection(), deadline=Deadline.after(0))
    assert result.dispatch is Dispatch.NOT_SENT
    assert result.carrier_failure is Failure.DEADLINE
    assert carrier.calls == 0


def test_nonce_and_canonical_response_are_required() -> None:
    response = encode_workload_shell_response(NONCE, WorkloadShellResponse(shell="/bin/sh"))
    with pytest.raises(WorkloadShellWireError):
        decode_workload_shell_response(response, "b" * 32)
    with pytest.raises(WorkloadShellWireError):
        decode_workload_shell_response(response + b" ", NONCE)


def test_response_limit_is_small_and_clears_oversized_capture() -> None:
    sink = _BoundedResponseSink()
    sink.try_write(memoryview(b"x" * MAX_WORKLOAD_SHELL_RESPONSE_BYTES))
    assert len(sink.data) == MAX_WORKLOAD_SHELL_RESPONSE_BYTES
    sink.try_write(memoryview(b"y"))
    assert sink.oversized
    assert not sink.data
    with pytest.raises(WorkloadShellWireError):
        decode_workload_shell_response(b"x" * (MAX_WORKLOAD_SHELL_RESPONSE_BYTES + 1), NONCE)


def test_large_valid_identity_request_remains_bounded() -> None:
    identity = IdentityExpectation(1, 1, tuple(range(65_536)))
    request = encode_workload_shell_request(WorkloadShellRequest(NONCE, identity))
    assert MAX_WORKLOAD_SHELL_RESPONSE_BYTES < len(request) <= MAX_WORKLOAD_SHELL_REQUEST_BYTES
    assert decode_workload_shell_request(request).identity == identity


@pytest.mark.skipif(sys.platform != "linux", reason="Linux identity helper")
def test_fixed_bundle_observes_current_user_shell() -> None:
    request = encode_workload_shell_request(WorkloadShellRequest(NONCE, _plan().expected))
    process = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", FIXED_SOURCE, NONCE],
        input=request,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert process.returncode == 0
    assert not process.stderr
    result = decode_workload_shell_response(process.stdout, NONCE)
    assert result.shell is not None or result.failure in {
        WorkloadShellFailure.MISSING,
        WorkloadShellFailure.UNSUPPORTED,
    }
