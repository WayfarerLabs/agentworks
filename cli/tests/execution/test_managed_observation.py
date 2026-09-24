"""Closed managed observation and capture exchanges across their private boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Generator
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_observation_guest as guest
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_store import FactName, ManagedJobStore, StoreError, Stream
from agentworks.execution._managed_observation_bundle import FIXED_BUNDLE
from agentworks.execution._managed_observation_exchange import (
    _MAX_RESPONSE_RECORDS,
    ManagedObservationIssue,
    ManagedObservationState,
    _Collector,
    observe_managed_run,
    read_managed_output,
)
from agentworks.execution._managed_observation_protocol import (
    FACT_ORDER,
    MAX_CONTROL_BYTES,
    MAX_REQUEST_BYTES,
    ManagedObservationError,
    ManagedObservationRequest,
    ManagedOperation,
    ManagedResultControl,
    checked_fact,
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

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed store")
RUN = "a" * 32
NONCE = "b" * 32


def _launch(
    *,
    target: str = "vm-one",
    boot: str = "00000000-0000-4000-8000-000000000001",
    incarnation: str = "v1:" + "c" * 64,
) -> bytes:
    return wire.encode_fact(
        {
            "version": 1,
            "kind": "launch",
            "run_id": RUN,
            "unit": f"agw-managed-{RUN}.service",
            "target": {"kind": "vm", "name": target, "incarnation": incarnation, "boot_id": boot},
            "workload": {"euid": 1001, "egid": 1001, "groups": [1001]},
            "shell": {"requested": None, "resolved_executable": None, "login": False, "interactive": False},
            "owner": {"kind": "resource", "owner_id": "session-7"},
            "lifetime": "independent",
            "profile_revision": 1,
            "receipt_namespace": "agentworks-managed-runs-v1",
            "receipt_protocol_version": 1,
        }
    )


def _fact(name: FactName, launch: bytes, *, disposition: str = "complete-capture", content: bytes = b"") -> bytes:
    common = {
        "version": 1,
        "run_id": RUN,
        "unit": f"agw-managed-{RUN}.service",
        "receipt_sha256": hashlib.sha256(launch).hexdigest(),
    }
    if name is FactName.WAIT:
        return wire.encode_fact({**common, "kind": "wait", "exit_code": 7, "signal": None})
    if name is FactName.BOUNDARY_EMPTY:
        return wire.encode_fact({**common, "kind": "boundary-empty"})
    return wire.encode_fact(
        {
            **common,
            "kind": "stream-end",
            "stream": "stdout" if name is FactName.STDOUT_END else "stderr",
            "retained_bytes": len(content),
            "retained_sha256": hashlib.sha256(content).hexdigest(),
            "disposition": disposition,
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> Generator[ManagedJobStore, None, None]:
    os.chmod(tmp_path, 0o700)
    anchor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with ManagedJobStore(RUN, _namespace="managed", _owner_uid=os.getuid(), _anchor_fd=anchor) as target:
            yield target
    finally:
        os.close(anchor)


def _request(
    operation: ManagedOperation = ManagedOperation.OBSERVE, stream: Stream | None = None
) -> ManagedObservationRequest:
    return ManagedObservationRequest(NONCE, operation, _launch(), IdentityExpectation(0, 0, (0,)), stream)


def _records(nonce: str, control: ManagedResultControl, facts: tuple[bytes, ...], output: bytes = b"") -> bytes:
    entries = [(FileRecordKind.RESULT, encode_result(control))]
    entries.extend((FileRecordKind.DATA, fact) for fact in facts)
    entries.extend((FileRecordKind.DATA, output[offset : offset + 4096]) for offset in range(0, len(output), 4096))
    entries.append((FileRecordKind.FINISHED, b""))
    return b"".join(
        encode_file_record(nonce, FileRecord(index, kind, body)) for index, (kind, body) in enumerate(entries)
    )


class ScriptedCarrier:
    def __init__(
        self,
        response: Callable[[ManagedObservationRequest], bytes],
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
        self.invocation: PreparedInvocation | None = None
        self.io: CarrierIO | None = None
        self.request: ManagedObservationRequest | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        self.invocation = invocation
        self.io = io
        assert isinstance(io.input, FiniteInput)
        assert isinstance(io.output, SinkOutput)
        assert io.input.data.startswith(FIXED_BUNDLE.prefix)
        request = decode_request(io.input.data[len(FIXED_BUNDLE.prefix) :])
        self.request = request
        payload = f"AGW_RUNTIME_1:{request.nonce}:ready:0\n".encode() + self.response(request)
        remaining = memoryview(payload)
        while remaining:
            consumed = io.output.stdout.try_write(remaining)
            assert consumed is not None and consumed > 0
            remaining = remaining[consumed:]
        if self.stderr:
            io.output.stderr.try_write(memoryview(self.stderr))
        completion = ExitStatus(code=self.code) if self.dispatch is Dispatch.SENT and self.code is not None else None
        channel = CapturedOutput(complete=self.complete, retention=Retention.DELIVERED)
        return CarrierReport(self.dispatch, completion, stdout=channel, stderr=channel)


def _plan() -> IdentityPlan:
    return IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT)


def _exchange(carrier: ScriptedCarrier, *, stream: Stream | None = None):  # type: ignore[no-untyped-def]
    kwargs = {
        "expected_launch": _launch(),
        "plan": _plan(),
        "deadline": Deadline.after(10),
        "runtime_selection": RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
    }
    if stream is None:
        return observe_managed_run(carrier, **kwargs)  # type: ignore[arg-type]
    return read_managed_output(carrier, stream=stream, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("mask", range(16))
def test_target_observe_preserves_every_fact_subset(store: ManagedJobStore, mask: int) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    for index, name in enumerate(FACT_ORDER[1:]):
        if mask & (1 << index):
            store.publish_fact(name, _fact(name, launch))
    prepared = guest._prepare(_request(), store)
    assert prepared.control.facts == tuple(
        name for name in FACT_ORDER if name is FactName.LAUNCH or store.read_fact(name)
    )
    assert prepared.facts[0] == launch


def test_request_codec_binds_exact_launch_and_rejects_arbitrary_selectors() -> None:
    request = _request(ManagedOperation.READ_OUTPUT, Stream.STDERR)
    encoded = encode_request(request)
    assert decode_request(encoded) == request
    assert len(encoded) <= MAX_REQUEST_BYTES
    for field, replacement in (
        ("run_id", "c" * 32),
        ("unit", "unowned.service"),
        ("receipt_sha256", "d" * 64),
        ("stream", "environment"),
        ("operation", "stop"),
    ):
        value = json.loads(encoded)
        value[field] = replacement
        with pytest.raises(ManagedObservationError):
            decode_request(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(ManagedObservationError):
        encode_request(_request(ManagedOperation.READ_OUTPUT))
    with pytest.raises(ManagedObservationError):
        decode_request(b" " + encoded)
    with pytest.raises(ManagedObservationError):
        decode_request(b"x" * (MAX_REQUEST_BYTES + 1))
    carrier = ScriptedCarrier(lambda _request: b"")
    with pytest.raises(ValidationError):
        read_managed_output(
            carrier,
            expected_launch=_launch(),
            stream="environment",  # type: ignore[arg-type]
            plan=_plan(),
            deadline=Deadline.after(10),
            runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        )
    assert carrier.calls == 0


@pytest.mark.parametrize("field", ["run_id", "unit", "receipt_sha256"])
def test_postlaunch_fact_rejects_wrong_run_unit_or_digest(field: str) -> None:
    launch = _launch()
    value = json.loads(_fact(FactName.WAIT, launch))
    if field == "run_id":
        value["run_id"] = "d" * 32
        value["unit"] = f"agw-managed-{'d' * 32}.service"
    elif field == "unit":
        value["unit"] = "unowned.service"
    else:
        value["receipt_sha256"] = "d" * 64
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ManagedObservationError):
        checked_fact(FactName.WAIT, data, launch)


@pytest.mark.parametrize(
    "variant", ["missing", "different-target", "different-boot", "different-incarnation", "malformed"]
)
def test_target_refuses_missing_conflicting_or_malformed_launch(
    store: ManagedJobStore, tmp_path: Path, variant: str
) -> None:
    if variant != "missing":
        stored = (
            _launch(target="vm-two")
            if variant == "different-target"
            else _launch(incarnation="v1:" + "d" * 64)
            if variant == "different-incarnation"
            else _launch(
                boot="00000000-0000-4000-8000-000000000002"
                if variant == "different-boot"
                else "00000000-0000-4000-8000-000000000001"
            )
        )
        store.publish_fact(FactName.LAUNCH, stored)
    if variant == "malformed":
        path = tmp_path / "managed" / RUN / "launch"
        os.chmod(path, 0o600)
        path.write_bytes(b"bad")
        os.chmod(path, 0o400)
    with pytest.raises((ManagedObservationError, StoreError)):
        guest._prepare(_request(), store)


@pytest.mark.parametrize("stream", [Stream.STDOUT, Stream.STDERR])
@pytest.mark.parametrize("source", [b"", b"abc", b"abcde"])
def test_target_read_closed_capture(store: ManagedJobStore, stream: Stream, source: bytes) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    writer = store.open_capture(stream, 3)
    writer.write(source)
    capture = writer.finish()
    name = FactName.STDOUT_END if stream is Stream.STDOUT else FactName.STDERR_END
    store.publish_fact(name, _fact(name, launch, disposition=capture.disposition.value, content=source[:3]))
    prepared = guest._prepare(_request(ManagedOperation.READ_OUTPUT, stream), store)
    assert prepared.control.facts == (FactName.LAUNCH, name)
    assert prepared.output == source[:3]


@pytest.mark.parametrize("condition", ["absent-end", "discard", "suppressed", "missing-spool"])
def test_target_output_unavailable_or_unknown(store: ManagedJobStore, condition: str) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    if condition != "absent-end":
        disposition = {"discard": "discarded", "suppressed": "sensitivity-suppressed"}.get(
            condition, "complete-capture"
        )
        store.publish_fact(FactName.STDOUT_END, _fact(FactName.STDOUT_END, launch, disposition=disposition))
    if condition == "missing-spool":
        with pytest.raises(StoreError):
            guest._prepare(_request(ManagedOperation.READ_OUTPUT, Stream.STDOUT), store)
    else:
        prepared = guest._prepare(_request(ManagedOperation.READ_OUTPUT, Stream.STDOUT), store)
        assert prepared.control.facts == (
            (FactName.LAUNCH,) if condition == "absent-end" else (FactName.LAUNCH, FactName.STDOUT_END)
        )
        assert prepared.output == b""


@pytest.mark.parametrize("stream", [Stream.STDOUT, Stream.STDERR])
def test_host_accepts_exact_observation_and_output_with_fixed_invocation(stream: Stream) -> None:
    launch = _launch()
    wait = _fact(FactName.WAIT, launch)
    carrier = ScriptedCarrier(
        lambda request: _records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, FactName.WAIT)),
            (launch, wait),
        )
    )
    candidate = _exchange(carrier)
    assert candidate.observation is not None
    assert candidate.observation.state is ManagedObservationState.OBSERVED
    assert candidate.observation.facts == ((FactName.LAUNCH, launch), (FactName.WAIT, wait))
    assert carrier.calls == 1
    assert carrier.invocation is not None and carrier.invocation.argv[:4] == ("/usr/bin/sudo", "-n", "--user=#0", "--")
    assert carrier.io is not None and carrier.io.sensitive
    assert carrier.request is not None and carrier.request.expected_launch == launch
    assert b"private-output-canary" not in repr(candidate).encode()

    output = b"private-output-canary"
    end_name = FactName.STDOUT_END if stream is Stream.STDOUT else FactName.STDERR_END
    end = _fact(end_name, launch, content=output)
    carrier = ScriptedCarrier(
        lambda request: _records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, end_name)),
            (launch, end),
            output,
        )
    )
    candidate = _exchange(carrier, stream=stream)
    assert candidate.observation is not None
    assert candidate.observation.state is ManagedObservationState.AVAILABLE
    assert candidate.observation.output == output
    assert output not in repr(candidate).encode()


@pytest.mark.parametrize(
    "fault",
    [
        "missing-terminal",
        "extra",
        "post-terminal",
        "malformed",
        "stderr",
        "incomplete",
        "nonzero",
        "lost",
        "unknown",
        "not-sent",
        "wrong-launch",
        "wrong-end",
    ],
)
def test_host_never_promotes_bad_or_uncertain_carrier_evidence(fault: str) -> None:
    launch = _launch()
    wait = _fact(FactName.WAIT, launch)

    def response(request: ManagedObservationRequest) -> bytes:
        data = _records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, FactName.WAIT)),
            (
                _launch(target="vm-other") if fault == "wrong-launch" else launch,
                _fact(FactName.WAIT, _launch(target="vm-other")) if fault == "wrong-end" else wait,
            ),
        )
        if fault == "missing-terminal":
            return data.rsplit(b"AGWF1", 1)[0]
        if fault == "extra":
            return data + b"unexpected\n"
        if fault == "post-terminal":
            return data + encode_file_record(request.nonce, FileRecord(4, FileRecordKind.DATA, b"x"))
        if fault == "malformed":
            return b"malformed\n"
        return data

    carrier = ScriptedCarrier(
        response,
        dispatch={"unknown": Dispatch.UNKNOWN, "not-sent": Dispatch.NOT_SENT}.get(fault, Dispatch.SENT),
        code=1 if fault == "nonzero" else None if fault == "lost" else 0,
        stderr=b"noise" if fault == "stderr" else b"",
        complete=fault != "incomplete",
    )
    candidate = _exchange(carrier)
    assert candidate.observation is not None
    assert candidate.observation.state not in (ManagedObservationState.OBSERVED, ManagedObservationState.AVAILABLE)
    assert candidate.observation.facts == ()
    assert candidate.observation.output is None


@pytest.mark.parametrize("disposition", ["discarded", "sensitivity-suppressed"])
def test_host_unavailable_requires_no_output_and_exact_end_binding(disposition: str) -> None:
    launch = _launch()
    discarded = _fact(FactName.STDOUT_END, launch, disposition=disposition)
    carrier = ScriptedCarrier(
        lambda request: _records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
            (launch, discarded),
        )
    )
    candidate = _exchange(carrier, stream=Stream.STDOUT)
    assert candidate.observation is not None
    assert candidate.observation.state is ManagedObservationState.UNAVAILABLE
    assert candidate.observation.facts == ((FactName.LAUNCH, launch), (FactName.STDOUT_END, discarded))
    assert candidate.observation.output is None
    pending = ScriptedCarrier(
        lambda request: _records(request.nonce, ManagedResultControl((FactName.LAUNCH,)), (launch,))
    )
    candidate = _exchange(pending, stream=Stream.STDOUT)
    assert candidate.observation is not None
    assert candidate.observation.state is ManagedObservationState.UNKNOWN
    assert candidate.observation.facts == ((FactName.LAUNCH, launch),)
    assert candidate.observation.output is None
    captured_empty = ScriptedCarrier(
        lambda request: _records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
            (launch, _fact(FactName.STDOUT_END, launch)),
        )
    )
    candidate = _exchange(captured_empty, stream=Stream.STDOUT)
    assert candidate.observation is not None and candidate.observation.state is ManagedObservationState.AVAILABLE
    assert candidate.observation.output == b""
    bad = ScriptedCarrier(
        lambda request: _records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
            (launch, _fact(FactName.STDOUT_END, launch, content=b"abc")),
            b"bad",
        )
    )
    candidate = _exchange(bad, stream=Stream.STDOUT)
    assert candidate.observation is not None
    assert candidate.observation.state is ManagedObservationState.INVALID
    assert candidate.observation.issue is ManagedObservationIssue.CONTENT


@pytest.mark.parametrize(
    ("disposition", "declared", "delivered", "state"),
    [
        ("truncated-capture", b"abc", b"abc", ManagedObservationState.AVAILABLE),
        ("complete-capture", b"abc", b"ab", ManagedObservationState.INCOMPLETE),
        ("complete-capture", b"abc", b"abcd", ManagedObservationState.INVALID),
        ("complete-capture", b"abc", b"abd", ManagedObservationState.INVALID),
        ("discarded", b"", b"x", ManagedObservationState.INVALID),
    ],
)
def test_host_derives_output_state_and_length_from_validated_end(
    disposition: str, declared: bytes, delivered: bytes, state: ManagedObservationState
) -> None:
    launch = _launch()
    end = _fact(FactName.STDOUT_END, launch, disposition=disposition, content=declared)
    carrier = ScriptedCarrier(
        lambda request: _records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
            (launch, end),
            delivered,
        )
    )
    candidate = _exchange(carrier, stream=Stream.STDOUT)
    assert candidate.observation is not None
    assert candidate.observation.state is state
    assert candidate.observation.output == (delivered if state is ManagedObservationState.AVAILABLE else None)


def test_host_refuses_stream_end_length_beyond_v1_bound() -> None:
    launch = _launch()
    end = json.loads(_fact(FactName.STDOUT_END, launch, content=b"x"))
    end["retained_bytes"] = wire.MAX_CAPTURE_PREFIX_BYTES_V1 + 1
    oversized = wire.encode_fact(end)
    carrier = ScriptedCarrier(
        lambda request: _records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
            (launch, oversized),
        )
    )
    candidate = _exchange(carrier, stream=Stream.STDOUT)
    assert candidate.observation is not None
    assert candidate.observation.state is ManagedObservationState.INVALID
    assert candidate.observation.output is None


def test_result_and_record_bounds() -> None:
    good = encode_result(ManagedResultControl((FactName.LAUNCH,)))
    assert decode_result(good).facts == (FactName.LAUNCH,)
    assert json.loads(good) == {"facts": ["launch"], "version": 1}
    for extra in ({"status": "available"}, {"output_bytes": 0}):
        with pytest.raises(ManagedObservationError):
            decode_result(json.dumps({**json.loads(good), **extra}, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(ManagedObservationError):
        decode_result(good + b" " * MAX_CONTROL_BYTES)
    with pytest.raises(ValueError):
        encode_file_record(NONCE, FileRecord(0, FileRecordKind.DATA, b"x" * 4097))


def test_response_record_count_has_a_fixed_bound() -> None:
    collector = _Collector(_request(ManagedOperation.READ_OUTPUT, Stream.STDOUT))
    collector.records = _MAX_RESPONSE_RECORDS
    collector.accept(FileRecord(_MAX_RESPONSE_RECORDS, FileRecordKind.DATA, b"x"))
    assert collector.issue is ManagedObservationIssue.CONTENT
