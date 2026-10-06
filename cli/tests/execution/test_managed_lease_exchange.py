"""Closed lease delivery, including packed Bookworm helpers with synthetic identity."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution import _managed_lease_bundle as bundle
from agentworks.execution import _managed_lease_exchange as exchange
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_wire import MAX_RECORD_BYTES, FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_bundle import _build_file_helper_bundle
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_lease_protocol import (
    ClockObservation,
    LeasePublication,
    LeaseRequest,
    decode_request,
    encode_result,
)
from agentworks.execution._managed_lease_store import read_lease
from agentworks.execution._managed_lease_wire import sampled_lease
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState, RuntimeSelection, RuntimeTargetOS
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

from ._bound_carrier_support import fixture_dispatch, run_fixture_process
from .test_managed_operation_lease import launch
from .test_managed_service_guest import _store
from .test_managed_start import GUEST, ROOT

PLAN = IdentityPlan(ROOT, IdentityMode.DIRECT)
RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX)


def _records(request: LeaseRequest, bodies: tuple[tuple[FileRecordKind, bytes], ...]) -> bytes:
    return b"".join(
        encode_file_record(request.nonce, FileRecord(index, kind, body)) for index, (kind, body) in enumerate(bodies)
    )


def _success(request: LeaseRequest) -> bytes:
    result = ClockObservation(1000) if request.lease is None else LeasePublication(request.lease.expires_ns)
    return _records(request, ((FileRecordKind.RESULT, encode_result(result)), (FileRecordKind.FINISHED, b"")))


class ScriptedCarrier:
    def __init__(self, response: Callable[[LeaseRequest], bytes] = _success) -> None:
        self.response = response
        self.calls = 0
        self.validations = 0
        self.requests: list[LeaseRequest] = []
        self.custody: LocalDeliveryCustody | None = None
        self.deadline: Deadline | None = None
        self.stderr = b""
        self.runtime = "ready"
        self.dispatch = Dispatch.SENT
        self.code = 0
        self.failure: Failure | None = None
        self.complete = True
        self.retention = Retention.DELIVERED
        self.unsettled = False
        self.refuse = False
        self.interrupt: BaseException | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.validations += 1
        if self.refuse:
            raise ValidationError("unsupported envelope")

    def execute(
        self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> CarrierReport:
        self.calls += 1
        self.custody, self.deadline = custody, deadline
        assert isinstance(io.input, FiniteInput)
        assert isinstance(io.output, SinkOutput)
        assert io.input.sensitive and io.sensitive
        request = decode_request(io.input.data[len(bundle.FIXED_BUNDLE.prefix) :])
        self.requests.append(request)
        payload = f"AGW_RUNTIME_1:{request.nonce}:{self.runtime}:0\n".encode() + self.response(request)
        pending = memoryview(payload)
        while pending:
            count = io.output.stdout.try_write(pending)
            assert count is not None and count > 0
            pending = pending[count:]
        io.output.stderr.try_write(memoryview(self.stderr))
        if self.unsettled:
            custody.begin_process()
        if self.interrupt is not None:
            raise self.interrupt
        channel = CapturedOutput(complete=self.complete, retention=self.retention)
        completion = ExitStatus(self.code) if self.dispatch is Dispatch.SENT else None
        return CarrierReport(self.dispatch, completion, 23, channel, channel, self.failure)


def _clock(
    carrier: ScriptedCarrier, custody: LocalDeliveryCustody, deadline: Deadline | None = None
) -> exchange.ManagedLeaseCandidate:
    return exchange.observe_operation_clock(
        carrier,
        plan=PLAN,
        guest=GUEST,
        runtime_selection=RUNTIME,
        deadline=deadline or Deadline.after(3),
        custody=custody,
    )


class PackedCarrier:
    """The retained common fixture closes exact custody before reading process evidence."""

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        pass

    def execute(
        self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> CarrierReport:
        try:
            result = run_fixture_process(
                list(invocation.argv),
                io=io,
                deadline=deadline,
                custody=custody,
                standalone_custody=custody,
            )
        finally:
            assert custody.close(Deadline.after(3))
        dispatch = fixture_dispatch(result)
        completion = (
            ExitStatus(result.exit_status) if dispatch is Dispatch.SENT and result.exit_status is not None else None
        )
        return CarrierReport(dispatch, completion, result.local_status, result.stdout, result.stderr, result.failure)


def test_two_deliveries_preserve_exact_store_deadline_and_separate_nonce() -> None:
    carrier, custody, deadline = ScriptedCarrier(), LocalDeliveryCustody(), Deadline.after(5)
    first = _clock(carrier, custody, deadline)
    receipt = launch()
    lease = sampled_lease(receipt, 1000)
    second = exchange.publish_operation_lease(
        carrier,
        expected_launch=receipt,
        lease=lease,
        plan=PLAN,
        guest=GUEST,
        runtime_selection=RUNTIME,
        deadline=deadline,
        custody=custody,
    )
    assert first.observation is not None and first.observation.result == ClockObservation(1000)
    assert second.observation is not None and second.observation.result == LeasePublication(lease.expires_ns)
    assert carrier.custody is custody and carrier.deadline is deadline
    assert carrier.requests[0].nonce != carrier.requests[1].nonce
    assert carrier.requests[1].expected_launch == receipt
    assert carrier.requests[1].lease == lease
    assert second.carrier_local_status == 23


@pytest.mark.parametrize(
    "fault", ["stderr", "nonzero", "failure", "incomplete", "retention", "unsettled", "unknown", "not_sent", "runtime"]
)
def test_carrier_and_custody_evidence_cannot_promote_helper_result(fault: str) -> None:
    carrier, custody = ScriptedCarrier(), LocalDeliveryCustody()
    if fault == "stderr":
        carrier.stderr = b"noise"
    elif fault == "nonzero":
        carrier.code = 9
    elif fault == "failure":
        carrier.failure = Failure.OBSERVATION
    elif fault == "incomplete":
        carrier.complete = False
    elif fault == "retention":
        carrier.retention = Retention.CAPTURED
    elif fault == "unsettled":
        carrier.unsettled = True
    elif fault == "runtime":
        carrier.runtime = "unsupported_version"
    else:
        carrier.dispatch = Dispatch.UNKNOWN if fault == "unknown" else Dispatch.NOT_SENT
    try:
        candidate = _clock(carrier, custody)
        assert candidate.dispatch is carrier.dispatch
        assert candidate.carrier_failure is carrier.failure
        if fault == "runtime":
            assert candidate.observation is None
        else:
            assert candidate.observation is not None and candidate.observation.result is None
    finally:
        assert custody.close(Deadline.after(3))


@pytest.mark.parametrize(
    "fault", ["kind", "data", "duplicate", "post", "failed_body", "missing", "oversized", "noise", "nonce", "sequence"]
)
def test_closed_order_control_and_record_bounds(fault: str) -> None:
    def response(request: LeaseRequest) -> bytes:
        result = (FileRecordKind.RESULT, encode_result(ClockObservation(1000)))
        terminal = (FileRecordKind.FINISHED, b"")
        if fault == "kind":
            result = (FileRecordKind.RESULT, encode_result(LeasePublication(1000)))
        if fault == "data":
            result = (FileRecordKind.DATA, b"unexpected")
        if fault == "failed_body":
            result = (FileRecordKind.FAILED, b"unexpected")
        if fault == "duplicate":
            return _records(request, (result, result, terminal))
        if fault == "post":
            return _records(request, (result, terminal, terminal))
        if fault == "missing":
            return _records(request, (result,))
        if fault == "oversized":
            return b"x" * (MAX_RECORD_BYTES + 1)
        if fault == "noise":
            return b"noise\n" + _success(request)
        if fault == "nonce":
            return _success(replace(request, nonce="f" * 32))
        if fault == "sequence":
            return encode_file_record(request.nonce, FileRecord(1, *result))
        return _records(request, (result, terminal))

    candidate = _clock(ScriptedCarrier(response), LocalDeliveryCustody())
    assert candidate.observation is not None
    assert candidate.observation.result is None and candidate.observation.issue is not None


def test_clean_failed_is_refusal_and_publication_must_acknowledge_requested_expiry() -> None:
    refusal = ScriptedCarrier(
        lambda request: _records(request, ((FileRecordKind.FAILED, b""), (FileRecordKind.FINISHED, b"")))
    )
    candidate = _clock(refusal, LocalDeliveryCustody())
    assert candidate.observation is not None and candidate.observation.state is exchange.ManagedLeaseState.REFUSED
    wrong = ScriptedCarrier(
        lambda request: _records(
            request, ((FileRecordKind.RESULT, encode_result(LeasePublication(1))), (FileRecordKind.FINISHED, b""))
        )
    )
    candidate = exchange.publish_operation_lease(
        wrong,
        expected_launch=launch(),
        lease=sampled_lease(launch(), 1000),
        plan=PLAN,
        guest=GUEST,
        runtime_selection=RUNTIME,
        deadline=Deadline.after(3),
        custody=LocalDeliveryCustody(),
    )
    assert candidate.observation is not None and candidate.observation.issue is exchange.ManagedLeaseIssue.CONTROL


@pytest.mark.parametrize("fault", ["boot", "run", "launch", "identity", "runtime", "deadline", "carrier", "unsettled"])
def test_validation_and_unsettled_delivery_refuse_before_dispatch(fault: str) -> None:
    carrier, custody = ScriptedCarrier(), LocalDeliveryCustody()
    receipt = launch()
    lease = sampled_lease(receipt, 1000)
    if fault == "run":
        lease = replace(lease, run_id="f" * 32)
    if fault == "launch":
        receipt = b"malformed"
    if fault == "carrier":
        carrier.refuse = True
    if fault == "unsettled":
        custody.begin_process()
    try:
        with pytest.raises((ValidationError, StateError)):
            exchange.publish_operation_lease(
                carrier,
                expected_launch=receipt,
                lease=lease,
                plan=replace(PLAN, expected=replace(ROOT, euid=1)) if fault == "identity" else PLAN,
                guest=replace(GUEST, init_start_ticks=GUEST.init_start_ticks + 1) if fault == "boot" else GUEST,
                runtime_selection=RuntimeSelection(RuntimeTargetOS.DARWIN) if fault == "runtime" else RUNTIME,
                deadline=Deadline(None) if fault == "deadline" else Deadline.after(3),
                custody=custody,
            )
        assert carrier.calls == 0
    finally:
        assert custody.close(Deadline.after(3))


def test_expired_deadline_validates_envelope_without_dispatch() -> None:
    carrier = ScriptedCarrier()
    candidate = _clock(carrier, LocalDeliveryCustody(), Deadline.after(0))
    assert carrier.validations == 1 and carrier.calls == 0
    assert candidate.carrier_failure is Failure.DEADLINE


def test_late_clean_reply_cannot_reset_shared_cycle_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter((0, 11))
    monkeypatch.setattr(time, "monotonic", lambda: next(times))
    carrier = ScriptedCarrier()
    candidate = _clock(carrier, LocalDeliveryCustody(), Deadline(10))
    assert carrier.calls == 1 and candidate.carrier_completion == ExitStatus(0)
    assert candidate.observation is not None and candidate.observation.result is None
    assert candidate.observation.issue is exchange.ManagedLeaseIssue.DEADLINE


@pytest.mark.parametrize("exception", [KeyboardInterrupt(), SystemExit(7)])
def test_original_base_exception_escapes_and_private_buffers_clear(
    exception: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    collectors: list[exchange._Collector] = []
    original = exchange._Collector

    def collect(request: LeaseRequest) -> exchange._Collector:
        collector = original(request)
        collectors.append(collector)
        return collector

    monkeypatch.setattr(exchange, "_Collector", collect)
    carrier = ScriptedCarrier()
    carrier.interrupt = exception
    with pytest.raises(type(exception)) as caught:
        _clock(carrier, LocalDeliveryCustody())
    assert caught.value is exception and collectors[0].result is None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux protected store and packed helper")
@pytest.mark.parametrize("fault", ["none", "guest", "identity", "stored_launch"])
def test_actual_packed_bookworm_clock_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    """Actual helper and store, with synthetic root/guest facts, not native root acceptance."""
    package = files("agentworks.execution")
    sources = tuple((name, package.joinpath(f"{name}.py").read_text()) for name in bundle._MODULES)
    injection = (
        "\nfrom pathlib import Path\n"
        f"_root=Path({str(tmp_path)!r})\n"
        "_real_store=ManagedJobStore\n"
        "def _test_store(run):\n"
        " fd=os.open(_root,os.O_RDONLY|os.O_DIRECTORY)\n"
        " try:return _real_store(run,_namespace='managed',_owner_uid=os.getuid(),_anchor_fd=fd)\n"
        " finally:os.close(fd)\n"
        "ManagedJobStore=_test_store\n"
        f"matches_current_identity=lambda identity:{fault != 'identity'!r}\n"
        "_test_protocol=__import__(__package__+'._vm_guest_identity_protocol',fromlist=['VMGuestIdentity'])\n"
        f"_identity=lambda:_test_protocol.VMGuestIdentity({GUEST.instance_marker!r},{GUEST.boot_id!r},"
        f"{GUEST.init_start_ticks + (fault == 'guest')})\n"
        "boottime_ns=lambda:1000\n"
    )
    sources = tuple(
        (name, source + injection if name == "_managed_lease_guest" else source) for name, source in sources
    )
    monkeypatch.setattr(
        exchange, "FIXED_BUNDLE", _build_file_helper_bundle("_agw_test_lease", sources, "_managed_lease_guest")
    )
    with _store(tmp_path) as store:
        stored = launch()
        if fault == "stored_launch":
            from agentworks.execution import _managed_job_wire as wire

            value = wire.decode_fact(stored)
            value["owner"] = {"kind": "operation", "owner_id": "f" * 32}
            stored = wire.encode_fact(value)
        store.publish_fact(FactName.LAUNCH, stored)
    carrier, custody = PackedCarrier(), LocalDeliveryCustody()
    try:
        clock = exchange.observe_operation_clock(
            carrier,
            plan=PLAN,
            guest=GUEST,
            runtime_selection=RUNTIME,
            deadline=Deadline.after(5),
            custody=custody,
        )
    finally:
        assert custody.close(Deadline.after(3))
    assert clock.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert clock.runtime_prerequisite.selected_path == "/usr/bin/python3"
    assert clock.observation is not None
    if fault in {"guest", "identity"}:
        assert clock.observation.state is exchange.ManagedLeaseState.REFUSED
    else:
        assert clock.observation.result == ClockObservation(1000)
    try:
        publication = exchange.publish_operation_lease(
            carrier,
            expected_launch=launch(),
            lease=sampled_lease(launch(), 1000),
            plan=PLAN,
            guest=GUEST,
            runtime_selection=RUNTIME,
            deadline=Deadline.after(5),
            custody=custody,
        )
    finally:
        assert custody.close(Deadline.after(3))
    assert publication.carrier_completion == ExitStatus(0)
    assert publication.observation is not None
    if fault == "none":
        assert publication.observation.state is exchange.ManagedLeaseState.PUBLISHED
        with _store(tmp_path) as store:
            assert read_lease(store, launch()) == sampled_lease(launch(), 1000)
    else:
        assert publication.observation.state is exchange.ManagedLeaseState.REFUSED
