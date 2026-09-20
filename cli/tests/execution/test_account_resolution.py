"""Host observation and real-helper checks for destination account resolution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._account import (
    AccountObservationError,
    AccountObservationState,
    AccountResolutionResult,
    resolve_account,
)
from agentworks.execution._account_bundle import FIXED_SOURCE
from agentworks.execution._account_protocol import (
    MAX_ACCOUNT_MESSAGE_BYTES,
    AccountFailure,
    encode_account_failure,
    encode_account_identity,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline import execute_inline_candidate, prepare_inline_candidate
from agentworks.execution._inline_control import FailureCode, FailurePhase
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
from agentworks.execution.carriers._subprocess import run_process
from agentworks.execution.models import Command


@dataclass
class TranscriptCarrier:
    transcript: bytes
    stderr: bytes = b""
    stdout_complete: bool = True
    stderr_complete: bool = True
    completion: ExitStatus | None = ExitStatus(code=0)
    failure: Failure | None = None
    calls: int = 0
    io: CarrierIO | None = None
    invocation: PreparedInvocation | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del deadline
        self.calls += 1
        self.io = io
        self.invocation = invocation
        assert isinstance(io.output, SinkOutput)
        io.output.stdout.try_write(memoryview(self.transcript))
        io.output.stderr.try_write(memoryview(self.stderr))
        stdout = CapturedOutput(complete=self.stdout_complete, retention=Retention.DELIVERED)
        stderr = CapturedOutput(complete=self.stderr_complete, retention=Retention.DELIVERED)
        return CarrierReport(
            Dispatch.SENT,
            self.completion,
            91,
            stdout,
            stderr,
            self.failure,
        )


class ReplyCarrier(TranscriptCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        nonce = invocation.argv[-1]
        self.transcript = encode_account_identity(nonce, IdentityExpectation(1001, 1002, (1002, 1003)))
        return super().execute(invocation, io=io, deadline=deadline)


class RefusalCarrier(TranscriptCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.transcript = encode_account_failure(invocation.argv[-1], AccountFailure.MISSING)
        return super().execute(invocation, io=io, deadline=deadline)


class ReflectedCarrier(TranscriptCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.transcript = json.dumps(
            {
                "account": "workload",
                "identity": {"egid": 1002, "euid": 1001, "groups": [1002]},
                "nonce": invocation.argv[-1],
                "status": "identity",
                "version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return super().execute(invocation, io=io, deadline=deadline)


class RaisingCarrier(TranscriptCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, deadline
        self.calls += 1
        self.io = io
        assert isinstance(io.output, SinkOutput)
        io.output.stdout.try_write(memoryview(b"private-response"))
        io.output.stderr.try_write(memoryview(b"private-diagnostic"))
        raise RuntimeError("carrier failed")


class LocalCarrier:
    def __init__(self) -> None:
        self.calls = 0
        self.io: CarrierIO | None = None
        self.invocation: PreparedInvocation | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        self.io = io
        self.invocation = invocation
        result = run_process(list(invocation.argv), io=io, deadline=deadline)
        completion = None
        if result.exit_status is not None:
            completion = (
                ExitStatus(signal=-result.exit_status)
                if result.exit_status < 0
                else ExitStatus(code=result.exit_status)
            )
        return CarrierReport(
            Dispatch.SENT if result.started else Dispatch.NOT_SENT,
            completion,
            result.local_status,
            result.stdout,
            result.stderr,
            result.failure,
        )


def _resolve(carrier: TranscriptCarrier | LocalCarrier, runtime_path: str = sys.executable) -> AccountResolutionResult:
    return resolve_account(carrier, "workload", Deadline.after(15), runtime_path)


def _current_account() -> str:
    import pwd

    return pwd.getpwuid(os.geteuid()).pw_name


def test_resolved_identity_uses_one_sensitive_payload_free_attempt_and_preserves_carrier_facts() -> None:
    carrier = ReplyCarrier(b"", completion=ExitStatus(code=19), failure=Failure.OBSERVATION)

    result = _resolve(carrier)

    assert carrier.calls == 1
    assert result.observation.state is AccountObservationState.RESOLVED
    assert result.observation.identity == IdentityExpectation(1001, 1002, (1002, 1003))
    assert result.carrier_completion == ExitStatus(code=19)
    assert result.carrier_local_status == 91
    assert result.carrier_failure is Failure.OBSERVATION
    assert carrier.io is not None and carrier.io.sensitive
    assert isinstance(carrier.io.input, FiniteInput) and carrier.io.input.sensitive
    assert carrier.io.input.data.isascii()
    assert json.loads(carrier.io.input.data)["account"] == "workload"
    assert carrier.invocation is not None and "workload" not in carrier.invocation.argv
    assert isinstance(carrier.io.output, SinkOutput)
    assert carrier.io.output.stdout.data == bytearray()  # type: ignore[attr-defined]


def test_typed_helper_refusal_is_distinct_from_invalid_observation() -> None:
    result = _resolve(RefusalCarrier(b""))

    assert result.observation.state is AccountObservationState.REFUSED
    assert result.observation.failure is AccountFailure.MISSING
    assert result.observation.error is None
    assert result.observation.identity is None


@pytest.mark.parametrize(
    ("transcript", "stderr", "stdout_complete", "stderr_complete", "error", "state"),
    [
        (b'{"truncated":', b"", True, True, AccountObservationError.RESPONSE, AccountObservationState.INVALID),
        (b"", b"", True, True, AccountObservationError.MISSING, AccountObservationState.INCOMPLETE),
        (
            b"x" * (MAX_ACCOUNT_MESSAGE_BYTES + 1),
            b"",
            True,
            True,
            AccountObservationError.OVERSIZED,
            AccountObservationState.INVALID,
        ),
        (b"{}", b"noise", True, True, AccountObservationError.STDERR, AccountObservationState.INVALID),
        (b"{}", b"", False, True, AccountObservationError.STREAMS, AccountObservationState.INCOMPLETE),
        (b"{}", b"", True, False, AccountObservationError.STREAMS, AccountObservationState.INCOMPLETE),
    ],
)
def test_noise_truncation_oversize_and_partial_streams_never_resolve(
    transcript: bytes,
    stderr: bytes,
    stdout_complete: bool,
    stderr_complete: bool,
    error: AccountObservationError,
    state: AccountObservationState,
) -> None:
    carrier = TranscriptCarrier(
        transcript,
        stderr=stderr,
        stdout_complete=stdout_complete,
        stderr_complete=stderr_complete,
    )

    result = _resolve(carrier)

    assert carrier.calls == 1
    assert result.observation.state is state
    assert result.observation.error is error
    assert result.observation.identity is None


def test_reflected_account_response_is_invalid() -> None:
    carrier = ReflectedCarrier(b"")

    result = _resolve(carrier)

    assert result.observation.state is AccountObservationState.INVALID
    assert result.observation.identity is None


def test_carrier_exception_clears_private_response_and_diagnostic_state() -> None:
    carrier = RaisingCarrier(b"")

    with pytest.raises(RuntimeError, match="carrier failed"):
        _resolve(carrier)

    assert carrier.calls == 1
    assert carrier.io is not None and isinstance(carrier.io.output, SinkOutput)
    assert carrier.io.output.stdout.data == bytearray()  # type: ignore[attr-defined]
    assert not carrier.io.output.stderr.saw_data  # type: ignore[attr-defined]


@pytest.mark.parametrize("account", ["", "bad\0account", "\ud800", "x" * MAX_ACCOUNT_MESSAGE_BYTES])
def test_invalid_or_oversized_trusted_account_refuses_before_dispatch(account: str) -> None:
    carrier = ReplyCarrier(b"")

    with pytest.raises(ValidationError):
        resolve_account(carrier, account, Deadline.after(1), sys.executable)

    assert carrier.calls == 0


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX account database")
def test_real_subprocess_resolves_current_and_missing_accounts() -> None:
    current = _current_account()
    carrier = LocalCarrier()

    found = resolve_account(carrier, current, Deadline.after(15), sys.executable)
    missing = resolve_account(
        carrier,
        "agw-account-that-does-not-exist-5e289461",
        Deadline.after(15),
        sys.executable,
    )

    assert carrier.calls == 2
    assert found.observation.state is AccountObservationState.RESOLVED
    assert found.observation.identity is not None
    assert missing.observation.state is AccountObservationState.REFUSED
    assert missing.observation.failure is AccountFailure.MISSING


@pytest.mark.skipif(sys.platform != "linux", reason="inline identity verification requires Linux")
def test_resolved_current_account_composes_with_truthful_direct_inline_identity_check() -> None:
    current = _current_account()
    carrier = LocalCarrier()
    resolved = resolve_account(carrier, current, Deadline.after(15), sys.executable)
    identity = resolved.observation.identity
    assert identity is not None
    actual = IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()})))

    prepared = prepare_inline_candidate(
        Command(["/bin/true"]),
        plan=IdentityPlan(identity, IdentityMode.DIRECT),
        runtime_path=sys.executable,
    )
    executed = execute_inline_candidate(carrier, prepared, deadline=Deadline.after(15))

    assert executed.observation.trusted_terminal
    if identity == actual:
        assert executed.observation.wait is not None
        assert executed.observation.wait.value == 0
        assert executed.observation.failure is None
    else:
        assert not executed.observation.launching
        assert executed.observation.wait is None
        assert executed.observation.failure is not None
        assert executed.observation.failure.phase is FailurePhase.IDENTITY
        assert executed.observation.failure.code is FailureCode.MISMATCH


def test_unavailable_interpreter_never_yields_identity() -> None:
    result = _resolve(LocalCarrier(), runtime_path="/definitely/missing/python3")

    assert result.observation.state in (AccountObservationState.INVALID, AccountObservationState.INCOMPLETE)
    assert result.observation.identity is None


def test_bookworm_python_311_bundle_smoke_if_available() -> None:
    interpreter = Path("/usr/bin/python3")
    if not interpreter.is_file():
        pytest.skip("/usr/bin/python3 is unavailable")
    version = subprocess.run(
        [str(interpreter), "-c", "import sys;print(sys.version_info[:2])"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if version.stdout.strip() != "(3, 11)":
        pytest.skip("/usr/bin/python3 is not the Bookworm Python 3.11 runtime")
    current = _current_account()

    result = resolve_account(LocalCarrier(), current, Deadline.after(15), str(interpreter))

    assert result.observation.state is AccountObservationState.RESOLVED
    assert result.observation.identity is not None
    assert FIXED_SOURCE.isascii()


@pytest.mark.windows
def test_host_import_does_not_require_posix_account_database() -> None:
    script = r"""
import sys

sys.modules["pwd"] = None
import agentworks.execution._account
assert sys.modules["pwd"] is None
"""
    completed = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, timeout=20)

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
