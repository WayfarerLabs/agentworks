"""Private managed start admission, staging, and one-shot carrier evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Generator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _managed_start_guest as guest
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_protocol import encode_managed_job_fact
from agentworks.execution._managed_job_request import ManagedJobRequest
from agentworks.execution._managed_job_store import FactName, ManagedJobStore, RequestAsset
from agentworks.execution._managed_runs import (
    ManagedLaunchState,
    ManagedOutputMode,
    ManagedOutputPolicy,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunRecord,
    ManagedRunRepository,
    ManagedRunSpec,
    ManagedShellIdentity,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from agentworks.execution._managed_service_bundle import FIXED_SOURCE
from agentworks.execution._managed_start_bundle import FIXED_BUNDLE
from agentworks.execution._managed_start_exchange import ManagedStartState, start_managed_run
from agentworks.execution._managed_start_protocol import (
    MAX_REQUEST_BYTES,
    ManagedStartError,
    ManagedStartRequest,
    ManagedStartResult,
    decode_request,
    decode_result,
    encode_request,
    encode_result,
)
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed store")
RUN = ManagedRunIdentity("a" * 32)
NONCE = "b" * 32
ROOT = IdentityExpectation(0, 0, (0,))


def _spec() -> ManagedRunSpec:
    return ManagedRunSpec(
        ManagedTargetIdentity(ManagedTargetKind.VM, "vm-one", "v1:" + "c" * 64, "00000000-0000-4000-8000-000000000001"),
        IdentityExpectation(1001, 1001, (1001,)),
        ManagedShellIdentity(None, None),
        ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "session-7"),
        ManagedRunLifetime.INDEPENDENT,
    )


def _request(record: ManagedRunRecord) -> ManagedJobRequest:
    return ManagedJobRequest(
        encode_managed_job_fact(ManagedRunReceipt(record.identity, record.identity.unit_name, record.spec)),
        "command",
        ("/usr/bin/true",),
        "/tmp",
        "capture",
        4096,
        (("TOKEN", "private-canary"),),
        b"",
        b"private-canary",
    )


@pytest.fixture
def reserved(tmp_path: Path) -> Generator[tuple[ManagedRunRepository, ManagedRunRecord], None, None]:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    record = repository.reserve(
        _spec(), output_policy=ManagedOutputPolicy(ManagedOutputMode.CAPTURE, 4096), identity=RUN
    )
    try:
        yield repository, record
    finally:
        database.close()


@pytest.fixture
def store(tmp_path: Path) -> Generator[ManagedJobStore, None, None]:
    os.chmod(tmp_path, 0o700)
    anchor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with ManagedJobStore(RUN.run_id, _namespace="managed", _owner_uid=os.getuid(), _anchor_fd=anchor) as target:
            yield target
    finally:
        os.close(anchor)


def _records(nonce: str, result: ManagedStartResult, launch: bytes | None = None) -> bytes:
    entries = [(FileRecordKind.RESULT, encode_result(result))]
    if launch is not None:
        entries.append((FileRecordKind.DATA, launch))
    entries.append((FileRecordKind.FINISHED, b""))
    return b"".join(
        encode_file_record(nonce, FileRecord(index, kind, body)) for index, (kind, body) in enumerate(entries)
    )


class ScriptedCarrier:
    def __init__(
        self,
        response: Callable[[ManagedStartRequest], bytes],
        *,
        dispatch: Dispatch = Dispatch.SENT,
        code: int | None = 0,
        stderr: bytes = b"",
        complete: bool = True,
    ) -> None:
        self.response = response
        self.dispatch = dispatch
        self.code = code
        self.stderr = stderr
        self.complete = complete
        self.calls = 0
        self.validations = 0
        self.invocation: PreparedInvocation | None = None
        self.io: CarrierIO | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.validations += 1

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        self.calls += 1
        self.invocation = invocation
        self.io = io
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        assert io.input.data.startswith(FIXED_BUNDLE.prefix)
        request = decode_request(io.input.data[len(FIXED_BUNDLE.prefix) :])
        payload = f"AGW_RUNTIME_1:{request.nonce}:ready:0\n".encode() + self.response(request)
        remaining = memoryview(payload)
        while remaining:
            written = io.output.stdout.try_write(remaining)
            assert written is not None and written > 0
            remaining = remaining[written:]
        if self.stderr:
            io.output.stderr.try_write(memoryview(self.stderr))
        completion = ExitStatus(code=self.code) if self.dispatch is Dispatch.SENT and self.code is not None else None
        channel = CapturedOutput(complete=self.complete, retention=Retention.DELIVERED)
        return CarrierReport(self.dispatch, completion, stdout=channel, stderr=channel)


def _start(repository: ManagedRunRepository, record: ManagedRunRecord, carrier: ScriptedCarrier):  # type: ignore[no-untyped-def]
    return start_managed_run(
        repository,
        record,
        carrier,
        request=_request(record),
        plan=IdentityPlan(ROOT, IdentityMode.SUDO_ROOT),
        deadline=Deadline.after(10),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
    )


def test_codec_is_canonical_bounded_and_secret_safe(reserved: tuple[ManagedRunRepository, ManagedRunRecord]) -> None:
    _, record = reserved
    request = ManagedStartRequest(NONCE, ROOT, _request(record))
    data = encode_request(request)
    assert decode_request(data) == request
    assert len(data) <= MAX_REQUEST_BYTES
    assert data.isascii()
    assert b"private-canary" not in data
    assert b"private-canary" not in repr(request).encode()
    binary = replace(request, job=replace(request.job, stdin=b"\x00\xff\x80"))
    binary_data = encode_request(binary)
    assert binary_data.isascii()
    assert decode_request(binary_data) == binary
    with pytest.raises(ManagedStartError):
        decode_request(data + b"x")
    with pytest.raises(ManagedStartError):
        decode_request(b" " + data)
    with pytest.raises(ManagedStartError):
        decode_request(b"x" * (MAX_REQUEST_BYTES + 1))
    value = json.loads(data)
    value["assets"][0] = value["assets"][0][:-1]
    with pytest.raises(ManagedStartError):
        decode_request(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    assert decode_result(encode_result(ManagedStartResult(None, None, (FactName.LAUNCH,)))).facts == (FactName.LAUNCH,)
    for facts in (["wait"], ["launch", "wait"]):
        with pytest.raises(ManagedStartError):
            decode_result(
                json.dumps(
                    {"version": 1, "exit_code": 0, "signal": None, "facts": facts},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )


def test_guest_stages_exact_assets_and_invokes_closed_service_argv(
    reserved: tuple[ManagedRunRepository, ManagedRunRecord], store: ManagedJobStore
) -> None:
    _, record = reserved
    request = ManagedStartRequest(NONCE, ROOT, _request(record))
    invocations: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> int:
        invocations.append(argv)
        store.publish_fact(FactName.LAUNCH, request.job.launch)
        return 0

    prepared = guest._prepare_start(request, store, python="/usr/bin/python3.11", runner=runner)
    assert prepared.result == ManagedStartResult(0, None, (FactName.LAUNCH,))
    assert prepared.launch == request.job.launch
    assert all(store.read_request_asset(name) is not None for name in RequestAsset)
    argv = invocations[0]
    assert argv[:6] == (
        "/usr/bin/systemd-run",
        "--system",
        "--quiet",
        "--no-ask-password",
        "--collect",
        f"--unit={RUN.unit_name}",
    )
    assert argv[6:14] == (
        "--service-type=notify",
        "--property=NotifyAccess=main",
        "--property=Delegate=yes",
        "--property=ExitType=main",
        "--property=KillMode=control-group",
        "--property=Restart=no",
        "--property=TimeoutStartSec=30s",
        "--property=TimeoutStopSec=5s",
    )
    assert argv[-8:] == ("--", "/usr/bin/python3.11", "-I", "-S", "-B", "-c", FIXED_SOURCE, RUN.run_id)
    assert "private-canary" not in repr(argv)
    with pytest.raises(ManagedStartError):
        guest._prepare_start(request, store, python="/usr/bin/python3.11", runner=runner)
    assert len(invocations) == 1


def test_guest_nonzero_or_timeout_preserves_assets_without_absence(
    reserved: tuple[ManagedRunRepository, ManagedRunRecord], store: ManagedJobStore
) -> None:
    _, record = reserved
    request = ManagedStartRequest(NONCE, ROOT, _request(record))
    prepared = guest._prepare_start(request, store, python="/usr/bin/python3.11", runner=lambda _argv: None)
    assert prepared.result == ManagedStartResult(None, None, ())
    assert prepared.launch is None
    assert all(store.read_request_asset(name) is not None for name in RequestAsset)


def test_systemd_client_stdio_and_environment_are_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, object] = {}

    def fake_run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        called.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert guest._run_systemd(("/usr/bin/systemd-run",)) == 0
    assert called == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        "timeout": 45.0,
        "check": False,
    }


@pytest.mark.parametrize("client_code", [0, 1, None])
@pytest.mark.parametrize("with_launch", [True, False])
def test_start_keeps_ack_separate_from_exact_receipt(
    reserved: tuple[ManagedRunRepository, ManagedRunRecord], client_code: int | None, with_launch: bool
) -> None:
    repository, record = reserved
    launch = _request(record).launch
    response = lambda request: _records(  # noqa: E731
        request.nonce,
        ManagedStartResult(client_code, None, (FactName.LAUNCH,) if with_launch else ()),
        launch if with_launch else None,
    )
    carrier = ScriptedCarrier(response)
    attempt = _start(repository, record, carrier)
    assert attempt.record.launch_state is (
        ManagedLaunchState.RECEIPT_CONFIRMED if with_launch else ManagedLaunchState.POSSIBLE_DISPATCH
    )
    assert attempt.candidate.observation is not None
    expected = (
        ManagedStartState.ACKNOWLEDGED
        if client_code == 0 and with_launch
        else ManagedStartState.INVALID
        if client_code == 0
        else ManagedStartState.UNCERTAIN
    )
    assert attempt.candidate.observation.state is expected
    assert attempt.candidate.observation.launch_fact == (launch if with_launch else None)
    assert carrier.validations == 2
    with pytest.raises((StateError, ValidationError)):
        _start(repository, record, ScriptedCarrier(response))


@pytest.mark.parametrize(
    "fault",
    [
        "not-sent",
        "unknown",
        "lost",
        "nonzero",
        "stderr",
        "incomplete",
        "extra",
        "repeat-data",
        "wrong-launch",
        "missing-terminal",
    ],
)
def test_carrier_fault_never_promotes_launch(
    reserved: tuple[ManagedRunRepository, ManagedRunRecord], fault: str
) -> None:
    repository, record = reserved
    launch = _request(record).launch

    def response(request: ManagedStartRequest) -> bytes:
        data = _records(
            request.nonce, ManagedStartResult(0, None, (FactName.LAUNCH,)), launch if fault != "wrong-launch" else b"x"
        )
        if fault == "extra":
            return data + b"noise\n"
        if fault == "repeat-data":
            return b"".join(
                encode_file_record(request.nonce, FileRecord(index, kind, body))
                for index, (kind, body) in enumerate(
                    (
                        (FileRecordKind.RESULT, encode_result(ManagedStartResult(0, None, (FactName.LAUNCH,)))),
                        (FileRecordKind.DATA, launch),
                        (FileRecordKind.DATA, launch),
                        (FileRecordKind.FINISHED, b""),
                    )
                )
            )
        if fault == "missing-terminal":
            return data.rsplit(b"AGWF1", 1)[0]
        return data

    carrier = ScriptedCarrier(
        response,
        dispatch={"not-sent": Dispatch.NOT_SENT, "unknown": Dispatch.UNKNOWN}.get(fault, Dispatch.SENT),
        code=None if fault == "lost" else 1 if fault == "nonzero" else 0,
        stderr=b"noise" if fault == "stderr" else b"",
        complete=fault != "incomplete",
    )
    attempt = _start(repository, record, carrier)
    assert attempt.record.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    assert attempt.candidate.observation is not None
    assert attempt.candidate.observation.state is not ManagedStartState.ACKNOWLEDGED
    assert attempt.candidate.observation.launch_fact is None


@pytest.mark.parametrize("fault", ["output", "target", "lifetime", "expired", "darwin", "nonroot"])
def test_preflight_refuses_without_mutating_reservation(
    reserved: tuple[ManagedRunRepository, ManagedRunRecord], fault: str
) -> None:
    repository, record = reserved
    request = _request(record)
    plan = IdentityPlan(ROOT, IdentityMode.SUDO_ROOT)
    runtime = RuntimeSelection(RuntimeTargetOS.LINUX)
    deadline = Deadline.after(10)
    if fault == "output":
        request = replace(request, capture_prefix_bytes=1)
    elif fault == "target":
        request = replace(request, launch=request.launch.replace(b"vm-one", b"vm-two"))
    elif fault == "lifetime":
        record = replace(
            record,
            spec=replace(
                record.spec,
                lifetime=ManagedRunLifetime.OPERATION,
                owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "d" * 32),
            ),
        )
    elif fault == "expired":
        deadline = Deadline.after(0)
    elif fault == "darwin":
        runtime = RuntimeSelection(RuntimeTargetOS.DARWIN)
    else:
        plan = IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DEMOTE)
    carrier = ScriptedCarrier(lambda _request: b"")
    with pytest.raises(ValidationError):
        start_managed_run(
            repository, record, carrier, request=request, plan=plan, deadline=deadline, runtime_selection=runtime
        )
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]
    assert carrier.calls == 0


def test_preflight_refuses_bogus_request_type_before_reservation_mutation(
    reserved: tuple[ManagedRunRepository, ManagedRunRecord],
) -> None:
    repository, record = reserved
    carrier = ScriptedCarrier(lambda _request: b"")
    with pytest.raises(ValidationError):
        start_managed_run(
            repository,
            record,
            carrier,
            request=object(),  # type: ignore[arg-type]
            plan=IdentityPlan(ROOT, IdentityMode.SUDO_ROOT),
            deadline=Deadline.after(10),
            runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
        )
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]
    assert carrier.validations == carrier.calls == 0


def test_proxmox_structural_refusal_precedes_possible_dispatch(
    reserved: tuple[ManagedRunRepository, ManagedRunRecord],
) -> None:
    repository, record = reserved
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.example", "node", 101, "operator!token", "secret"))
    request = replace(_request(record), stdin=b"x" * 50_000)
    with pytest.raises(ValidationError):
        start_managed_run(
            repository,
            record,
            carrier,
            request=request,
            plan=IdentityPlan(ROOT, IdentityMode.SUDO_ROOT),
            deadline=Deadline.after(10),
            runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
        )
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]


def test_unexpected_carrier_exception_propagates_after_possible_dispatch(
    reserved: tuple[ManagedRunRepository, ManagedRunRecord],
) -> None:
    repository, record = reserved

    class BrokenCarrier(ScriptedCarrier):
        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            self.calls += 1
            raise RuntimeError("unexpected carrier failure")

    carrier = BrokenCarrier(lambda _request: b"")
    with pytest.raises(RuntimeError):
        _start(repository, record, carrier)
    assert carrier.calls == 1
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
