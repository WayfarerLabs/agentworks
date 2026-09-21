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
from agentworks.execution import _account
from agentworks.execution._account import (
    AccountObservationError,
    AccountObservationState,
    AccountResolutionResult,
    FileOwnershipObservationError,
    FileOwnershipObservationState,
    FileOwnershipResolutionResult,
    resolve_account,
    resolve_file_ownership,
)
from agentworks.execution._account_bundle import FIXED_SOURCE
from agentworks.execution._account_protocol import (
    MAX_ACCOUNT_MESSAGE_BYTES,
    AccountFailure,
    FileOwnership,
    FileOwnershipFailure,
    FileOwnershipRequestError,
    encode_account_failure,
    encode_account_identity,
    encode_file_ownership_failure,
    encode_file_ownership_success,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline import execute_inline_candidate, prepare_inline_candidate
from agentworks.execution._inline_control import FailureCode, FailurePhase
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
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
from agentworks.execution.carriers._subprocess import run_process
from agentworks.execution.models import Command

_HOST_TARGET = RuntimeTargetOS.DARWIN if sys.platform == "darwin" else RuntimeTargetOS.LINUX


def _runtime_selection(path: str = sys.executable) -> RuntimeSelection:
    return RuntimeSelection(_HOST_TARGET, path)


def _nonce(invocation: PreparedInvocation) -> str:
    marker = invocation.argv.index("agentworks-runtime-prerequisite")
    return invocation.argv[marker + 1]


def _ready_record(invocation: PreparedInvocation) -> bytes:
    return f"AGW_RUNTIME_1:{_nonce(invocation)}:ready:0\n".encode("ascii")


def _exception_details(error: BaseException) -> str:
    pending = [error]
    seen: set[int] = set()
    details: list[object] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        details.extend(current.args)
        details.append(getattr(current, "object", None))
        details.append(getattr(current, "doc", None))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return repr(details)


@dataclass
class TranscriptCarrier:
    transcript: bytes
    stderr: bytes = b""
    prefix_ready: bool = True
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
        transcript = _ready_record(invocation) + self.transcript if self.prefix_ready else self.transcript
        io.output.stdout.try_write(memoryview(transcript))
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
        nonce = _nonce(invocation)
        self.transcript = encode_account_identity(nonce, IdentityExpectation(1001, 1002, (1002, 1003)))
        return super().execute(invocation, io=io, deadline=deadline)


class RefusalCarrier(TranscriptCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.transcript = encode_account_failure(_nonce(invocation), AccountFailure.MISSING)
        return super().execute(invocation, io=io, deadline=deadline)


class ReflectedCarrier(TranscriptCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.transcript = json.dumps(
            {
                "account": "workload",
                "identity": {"egid": 1002, "euid": 1001, "groups": [1002]},
                "nonce": _nonce(invocation),
                "status": "identity",
                "version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return super().execute(invocation, io=io, deadline=deadline)


class OwnershipReplyCarrier(TranscriptCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.transcript = encode_file_ownership_success(_nonce(invocation), FileOwnership(1001, 2003))
        return super().execute(invocation, io=io, deadline=deadline)


class OwnershipRefusalCarrier(TranscriptCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.transcript = encode_file_ownership_failure(
            _nonce(invocation),
            FileOwnershipFailure.MISSING_GROUP,
        )
        return super().execute(invocation, io=io, deadline=deadline)


class WrongNonceOwnershipCarrier(TranscriptCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.transcript = encode_file_ownership_success("f" * 32, FileOwnership(1001, 2003))
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
    return resolve_account(carrier, "workload", Deadline.after(15), _runtime_selection(runtime_path))


def _resolve_ownership(
    carrier: TranscriptCarrier | LocalCarrier,
    runtime_path: str = sys.executable,
) -> FileOwnershipResolutionResult:
    return resolve_file_ownership(
        carrier,
        "workload",
        "tmux-agent-access",
        Deadline.after(15),
        _runtime_selection(runtime_path),
    )


def _current_account() -> str:
    import pwd

    return pwd.getpwuid(os.geteuid()).pw_name


def _current_group() -> str:
    import grp

    return grp.getgrgid(os.getegid()).gr_name


def test_resolved_identity_uses_one_sensitive_payload_free_attempt_and_preserves_carrier_facts() -> None:
    account = "account-payload-canary-c86d89e5"
    carrier = ReplyCarrier(b"", completion=ExitStatus(code=19), failure=Failure.OBSERVATION)

    result = resolve_account(carrier, account, Deadline.after(15), _runtime_selection())

    assert carrier.calls == 1
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.observation is not None
    assert result.observation.state is AccountObservationState.RESOLVED
    assert result.observation.identity == IdentityExpectation(1001, 1002, (1002, 1003))
    assert result.carrier_completion == ExitStatus(code=19)
    assert result.carrier_local_status == 91
    assert result.carrier_failure is Failure.OBSERVATION
    assert carrier.io is not None and carrier.io.sensitive
    assert isinstance(carrier.io.input, FiniteInput) and carrier.io.input.sensitive
    assert carrier.io.input.data.isascii()
    assert json.loads(carrier.io.input.data)["account"] == account
    assert carrier.invocation is not None
    assert all(account not in argument for argument in carrier.invocation.argv)
    assert isinstance(carrier.io.output, SinkOutput)
    assert carrier.io.output.stdout.downstream.data == bytearray()  # type: ignore[attr-defined]


def test_typed_helper_refusal_is_distinct_from_invalid_observation() -> None:
    result = _resolve(RefusalCarrier(b""))

    assert result.observation is not None
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
    assert result.observation is not None
    assert result.observation.state is state
    assert result.observation.error is error
    assert result.observation.identity is None


def test_reflected_account_response_is_invalid() -> None:
    carrier = ReflectedCarrier(b"")

    result = _resolve(carrier)

    assert result.observation is not None
    assert result.observation.state is AccountObservationState.INVALID
    assert result.observation.identity is None


def test_carrier_exception_clears_private_response_and_diagnostic_state() -> None:
    carrier = RaisingCarrier(b"")

    with pytest.raises(RuntimeError, match="carrier failed"):
        _resolve(carrier)

    assert carrier.calls == 1
    assert carrier.io is not None and isinstance(carrier.io.output, SinkOutput)
    assert carrier.io.output.stdout.downstream.data == bytearray()  # type: ignore[attr-defined]
    assert not carrier.io.output.stderr.saw_data  # type: ignore[attr-defined]


def test_resolved_file_ownership_uses_one_sensitive_name_free_attempt_and_preserves_carrier_facts() -> None:
    owner = "owner-payload-canary-3d8aa7f4"
    group = "group-payload-canary-e8a209bf"
    carrier = OwnershipReplyCarrier(
        b"",
        completion=ExitStatus(code=23),
        failure=Failure.OBSERVATION,
    )

    result = resolve_file_ownership(carrier, owner, group, Deadline.after(15), _runtime_selection())

    assert carrier.calls == 1
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.observation is not None
    assert result.observation.state is FileOwnershipObservationState.RESOLVED
    assert result.observation.ownership == FileOwnership(1001, 2003)
    assert result.carrier_completion == ExitStatus(code=23)
    assert result.carrier_local_status == 91
    assert result.carrier_failure is Failure.OBSERVATION
    assert carrier.io is not None and carrier.io.sensitive
    assert isinstance(carrier.io.input, FiniteInput) and carrier.io.input.sensitive
    request = json.loads(carrier.io.input.data)
    assert request["owner"] == owner
    assert request["group"] == group
    assert carrier.invocation is not None
    assert all(owner not in argument and group not in argument for argument in carrier.invocation.argv)
    assert isinstance(carrier.io.output, SinkOutput)
    assert carrier.io.output.stdout.downstream.data == bytearray()  # type: ignore[attr-defined]


def test_file_ownership_refusal_distinguishes_missing_group() -> None:
    result = _resolve_ownership(OwnershipRefusalCarrier(b""))

    assert result.observation is not None
    assert result.observation.state is FileOwnershipObservationState.REFUSED
    assert result.observation.failure is FileOwnershipFailure.MISSING_GROUP
    assert result.observation.error is None
    assert result.observation.ownership is None


@pytest.mark.parametrize(
    ("carrier", "error", "state"),
    [
        (
            TranscriptCarrier(b'{"truncated":'),
            FileOwnershipObservationError.RESPONSE,
            FileOwnershipObservationState.INVALID,
        ),
        (
            TranscriptCarrier(b""),
            FileOwnershipObservationError.MISSING,
            FileOwnershipObservationState.INCOMPLETE,
        ),
        (
            TranscriptCarrier(b"x" * (MAX_ACCOUNT_MESSAGE_BYTES + 1)),
            FileOwnershipObservationError.OVERSIZED,
            FileOwnershipObservationState.INVALID,
        ),
        (
            TranscriptCarrier(b"{}", stderr=b"noise"),
            FileOwnershipObservationError.STDERR,
            FileOwnershipObservationState.INVALID,
        ),
        (
            TranscriptCarrier(b"{}", stdout_complete=False),
            FileOwnershipObservationError.STREAMS,
            FileOwnershipObservationState.INCOMPLETE,
        ),
        (
            TranscriptCarrier(b"{}", stderr_complete=False),
            FileOwnershipObservationError.STREAMS,
            FileOwnershipObservationState.INCOMPLETE,
        ),
        (
            WrongNonceOwnershipCarrier(b""),
            FileOwnershipObservationError.RESPONSE,
            FileOwnershipObservationState.INVALID,
        ),
        (
            ReplyCarrier(b""),
            FileOwnershipObservationError.RESPONSE,
            FileOwnershipObservationState.INVALID,
        ),
    ],
)
def test_file_ownership_noise_incomplete_wrong_nonce_and_wrong_kind_never_resolve(
    carrier: TranscriptCarrier,
    error: FileOwnershipObservationError,
    state: FileOwnershipObservationState,
) -> None:
    result = _resolve_ownership(carrier)

    assert carrier.calls == 1
    assert result.observation is not None
    assert result.observation.state is state
    assert result.observation.error is error
    assert result.observation.ownership is None


def test_file_ownership_carrier_exception_clears_private_sinks() -> None:
    carrier = RaisingCarrier(b"")

    with pytest.raises(RuntimeError, match="carrier failed"):
        _resolve_ownership(carrier)

    assert carrier.calls == 1
    assert carrier.io is not None and isinstance(carrier.io.output, SinkOutput)
    assert carrier.io.output.stdout.downstream.data == bytearray()  # type: ignore[attr-defined]
    assert not carrier.io.output.stderr.saw_data  # type: ignore[attr-defined]


@pytest.mark.parametrize("account", ["", "bad\0account", "\ud800", "x" * MAX_ACCOUNT_MESSAGE_BYTES])
def test_invalid_or_oversized_trusted_account_refuses_before_dispatch(account: str) -> None:
    carrier = ReplyCarrier(b"")

    with pytest.raises(ValidationError):
        resolve_account(carrier, account, Deadline.after(1), _runtime_selection())

    assert carrier.calls == 0


@pytest.mark.parametrize(
    ("owner", "group"),
    [
        ("", "group"),
        ("owner", ""),
        ("bad\0owner", "group"),
        ("owner", "bad\0group"),
        ("\ud800", "group"),
        ("owner", "\ud800"),
        ("x" * MAX_ACCOUNT_MESSAGE_BYTES, "group"),
        ("owner", "x" * MAX_ACCOUNT_MESSAGE_BYTES),
    ],
)
def test_invalid_or_oversized_file_ownership_names_refuse_before_dispatch(owner: str, group: str) -> None:
    carrier = OwnershipReplyCarrier(b"")

    with pytest.raises(ValidationError):
        resolve_file_ownership(carrier, owner, group, Deadline.after(1), _runtime_selection())

    assert carrier.calls == 0


def test_file_ownership_host_conversion_discards_sensitive_encoder_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "ownership-host-chain-canary"

    def fail(_request: object) -> bytes:
        raw_error: UnicodeEncodeError | None = None
        try:
            ("\ud800" + canary).encode("utf-8")
        except UnicodeEncodeError as error:
            raw_error = error
        assert raw_error is not None
        wrapped = FileOwnershipRequestError(FileOwnershipFailure.INVALID_REQUEST)
        wrapped.__context__ = raw_error
        raise wrapped

    monkeypatch.setattr(_account, "encode_file_ownership_request", fail)
    carrier = OwnershipReplyCarrier(b"")

    with pytest.raises(ValidationError) as raised:
        resolve_file_ownership(carrier, "owner", "group", Deadline.after(1), _runtime_selection())

    assert canary not in _exception_details(raised.value)
    assert carrier.calls == 0


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires a POSIX account database")
def test_real_subprocess_resolves_current_and_missing_accounts() -> None:
    current = _current_account()
    carrier = LocalCarrier()

    found = resolve_account(carrier, current, Deadline.after(15), _runtime_selection())
    missing = resolve_account(
        carrier,
        "agw-account-that-does-not-exist-5e289461",
        Deadline.after(15),
        _runtime_selection(),
    )

    assert carrier.calls == 2
    assert found.observation is not None
    assert missing.observation is not None
    assert found.observation.state is AccountObservationState.RESOLVED
    assert found.observation.identity is not None
    assert missing.observation.state is AccountObservationState.REFUSED
    assert missing.observation.failure is AccountFailure.MISSING


@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="requires POSIX account databases")
def test_real_subprocess_resolves_current_file_owner_and_group_and_missing_names() -> None:
    current_owner = _current_account()
    current_group = _current_group()
    carrier = LocalCarrier()

    found = resolve_file_ownership(
        carrier,
        current_owner,
        current_group,
        Deadline.after(15),
        _runtime_selection(),
    )
    missing_owner = resolve_file_ownership(
        carrier,
        "agw-owner-that-does-not-exist-51635510",
        current_group,
        Deadline.after(15),
        _runtime_selection(),
    )
    missing_group = resolve_file_ownership(
        carrier,
        current_owner,
        "agw-group-that-does-not-exist-18c3fd0b",
        Deadline.after(15),
        _runtime_selection(),
    )

    assert carrier.calls == 3
    assert found.observation is not None
    assert missing_owner.observation is not None
    assert missing_group.observation is not None
    assert found.observation.state is FileOwnershipObservationState.RESOLVED
    assert found.observation.ownership == FileOwnership(os.geteuid(), os.getegid())
    assert missing_owner.observation.failure is FileOwnershipFailure.MISSING_OWNER
    assert missing_group.observation.failure is FileOwnershipFailure.MISSING_GROUP


@pytest.mark.skipif(sys.platform != "linux", reason="inline identity verification requires Linux")
def test_resolved_current_account_composes_with_truthful_direct_inline_identity_check() -> None:
    current = _current_account()
    carrier = LocalCarrier()
    resolved = resolve_account(carrier, current, Deadline.after(15), _runtime_selection())
    assert resolved.observation is not None
    identity = resolved.observation.identity
    assert identity is not None
    actual = IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()})))

    prepared = prepare_inline_candidate(
        Command(["/bin/true"]),
        plan=IdentityPlan(identity, IdentityMode.DIRECT),
        runtime_selection=_runtime_selection(),
    )
    executed = execute_inline_candidate(carrier, prepared, deadline=Deadline.after(15))

    assert executed.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert executed.observation is not None
    observation = executed.observation
    assert observation.trusted_terminal
    if identity == actual:
        assert observation.wait is not None
        assert observation.wait.value == 0
        assert observation.failure is None
    else:
        assert not observation.launching
        assert observation.wait is None
        assert observation.failure is not None
        assert observation.failure.phase is FailurePhase.IDENTITY
        assert observation.failure.code is FailureCode.MISMATCH


def test_unavailable_interpreter_never_yields_identity() -> None:
    result = _resolve(LocalCarrier(), runtime_path="/definitely/missing/python3")

    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
    assert result.observation is None


def test_unavailable_interpreter_never_yields_file_ownership() -> None:
    result = _resolve_ownership(LocalCarrier(), runtime_path="/definitely/missing/python3")

    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
    assert result.observation is None


def test_complete_runtime_refusal_survives_input_failure_without_operation_observation() -> None:
    class RuntimeRefusalCarrier(TranscriptCarrier):
        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            self.transcript = f"AGW_RUNTIME_1:{_nonce(invocation)}:missing:-\n".encode("ascii")
            return super().execute(invocation, io=io, deadline=deadline)

    carrier = RuntimeRefusalCarrier(
        b"",
        prefix_ready=False,
        stdout_complete=False,
        failure=Failure.INPUT,
    )

    result = _resolve(carrier)

    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
    assert result.observation is None
    assert result.carrier_failure is Failure.INPUT


def test_input_failure_without_runtime_record_is_unknown_not_missing() -> None:
    result = _resolve(
        TranscriptCarrier(
            b"",
            prefix_ready=False,
            stdout_complete=False,
            failure=Failure.INPUT,
        )
    )

    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.UNKNOWN
    assert result.observation is None
    assert result.carrier_failure is Failure.INPUT


def test_hostile_python_environment_cannot_write_or_change_account_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup = tmp_path / "startup.py"
    startup.write_text(f"open({os.fspath(tmp_path / 'startup-ran')!r},'w').close()\n")
    injected = tmp_path / "injected"
    injected.mkdir()
    (injected / "sitecustomize.py").write_text(f"open({os.fspath(tmp_path / 'sitecustomize-ran')!r},'w').close()\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONHOME", os.fspath(tmp_path / "bad-home"))
    monkeypatch.setenv("PYTHONPATH", os.fspath(injected))
    monkeypatch.setenv("PYTHONSTARTUP", os.fspath(startup))
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", os.fspath(tmp_path / "cache"))
    before = {entry.relative_to(tmp_path) for entry in tmp_path.rglob("*")}

    result = resolve_account(
        LocalCarrier(),
        _current_account(),
        Deadline.after(15),
        _runtime_selection(),
    )

    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.observation is not None
    assert result.observation.state is AccountObservationState.RESOLVED
    assert {entry.relative_to(tmp_path) for entry in tmp_path.rglob("*")} == before


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
    carrier = LocalCarrier()

    result = resolve_account(
        carrier,
        current,
        Deadline.after(15),
        RuntimeSelection(RuntimeTargetOS.LINUX),
    )

    assert result.runtime_prerequisite == RuntimePrerequisiteObservation(
        RuntimePrerequisiteState.READY,
        str(interpreter),
    )
    assert result.observation is not None
    assert result.observation.state is AccountObservationState.RESOLVED
    assert result.observation.identity is not None
    assert carrier.calls == 1
    assert FIXED_SOURCE.isascii()


def test_bookworm_python_311_bundle_resolves_file_ownership_if_available() -> None:
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

    carrier = LocalCarrier()
    result = resolve_file_ownership(
        carrier,
        _current_account(),
        _current_group(),
        Deadline.after(15),
        RuntimeSelection(RuntimeTargetOS.LINUX),
    )

    assert result.runtime_prerequisite == RuntimePrerequisiteObservation(
        RuntimePrerequisiteState.READY,
        str(interpreter),
    )
    assert result.observation is not None
    assert result.observation.state is FileOwnershipObservationState.RESOLVED
    assert result.observation.ownership == FileOwnership(os.geteuid(), os.getegid())
    assert carrier.calls == 1


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
