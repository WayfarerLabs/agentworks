"""Passive body probes cannot become actual managed-start authority."""

from __future__ import annotations

import gc
import hashlib
import json
import weakref
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _managed_start_exchange as exchange
from agentworks.execution import _managed_start_preparation as preparation
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_lease_wire import (
    MAX_CLOCK_NS,
    MAX_LEASE_BYTES,
    WINDOW_NS,
    OperationLease,
    sampled_lease,
)
from agentworks.execution._managed_runs import (
    ManagedOutputMode,
    ManagedOutputPolicy,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunRepository,
)
from agentworks.execution._managed_start_bundle import FIXED_BUNDLE
from agentworks.execution._managed_start_preparation import prepare_managed_start_body
from agentworks.execution._managed_start_protocol import (
    ManagedStartResult,
    decode_request,
    encode_body,
    encode_result,
    encode_tail,
)
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._scratch import ScratchReference
from agentworks.execution._scratch_receipt import ScratchOperation, ScratchOwnership, ScratchReceiptContext, _Identity
from agentworks.execution._staged_helper_protocol import StagedHelperRequest
from agentworks.execution._staged_helper_protocol import decode_request as decode_staged_request
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
from agentworks.execution.carriers.proxmox import ProxmoxCarrier

from .test_managed_start_operation import GUEST, ROOT, RUN, _planned_request, _spec

PLAN = IdentityPlan(ROOT, IdentityMode.SUDO_ROOT)
RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
POLICY = ManagedOutputPolicy(ManagedOutputMode.CAPTURE, 4096)


class Carrier:
    def __init__(self, *, staged: bool = False, proxmox: bool = False) -> None:
        self.staged, self.proxmox = staged, proxmox
        self.validations: list[tuple[PreparedInvocation, CarrierIO]] = []
        self.calls = 0
        self.control: BaseException | None = None
        self.launch = b""
        self.noise = False
        self.code = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.validations.append((invocation, io))
        if self.control is not None:
            raise self.control
        if self.staged and len(self.validations) == 1:
            raise ValidationError("inline envelope refused")
        if self.proxmox:
            ProxmoxCarrier._request_body(invocation, io)

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        self.calls += 1
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        control, _ = json.JSONDecoder().raw_decode(io.input.data[io.input.data.index(b"{") :].decode("ascii"))
        nonce = control["nonce"]
        records = b"".join(
            encode_file_record(nonce, FileRecord(index, kind, data))
            for index, (kind, data) in enumerate(
                (
                    (FileRecordKind.RESULT, encode_result(ManagedStartResult(0, None, (FactName.LAUNCH,)))),
                    (FileRecordKind.DATA, self.launch),
                    (FileRecordKind.FINISHED, b""),
                )
            )
        )
        io.output.stdout.try_write(memoryview(f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode() + records))
        if self.noise:
            io.output.stderr.try_write(memoryview(b"noise"))
        stream = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(self.code), stdout=stream, stderr=stream)


def facts(*, operation: bool = True, size: int = 0):
    spec = _spec()
    if operation:
        spec = replace(
            spec,
            lifetime=ManagedRunLifetime.OPERATION,
            owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "f" * 32),
        )
    request = replace(_planned_request(RUN, spec), stdin=b"x" * size)
    return spec, request


def prepare(carrier: Carrier, *, operation: bool = True, size: int = 0, deadline: Deadline | None = None):
    spec, request = facts(operation=operation, size=size)
    carrier.launch = request.launch
    return prepare_managed_start_body(
        carrier, RUN, spec, POLICY, request, PLAN, deadline or Deadline.after(30), RUNTIME, GUEST
    )


def activation(body, **changes) -> StagedHelperRequest:
    token = b"t" * 16
    reference = ScratchReference(
        ScratchOwnership(
            token,
            ScratchReceiptContext(ScratchOperation.STAGE, ROOT),
            _Identity(1, 2),
            _Identity(1, 3),
            _Identity(1, 4),
            0,
            body.expected_length,
            _Identity(1, 5),
        )
    )
    request = StagedHelperRequest(body.nonce, ROOT, token, reference, body.digest, 10_000_000_000)
    return replace(request, **changes)


def test_inline_probe_is_passive_and_not_a_claimable_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    carrier = Carrier()

    def forbidden(*args, **kwargs):
        pytest.fail("probe created an executable attempt")

    with monkeypatch.context() as scope:
        scope.setattr(exchange, "_PreparedAttempt", forbidden)
        body = prepare(carrier)
    try:
        assert not body.staged and carrier.calls == 0 and len(carrier.validations) == 1
        assert not hasattr(body, "claim") and not hasattr(body, "io")
        assert body.parts[0] == FIXED_BUNDLE.prefix and body.expected_length == sum(map(len, body.parts)) + 512
        invocation, io = carrier.validations[0]
        assert isinstance(io.input, FiniteInput)
        probe = decode_request(io.input.data[len(FIXED_BUNDLE.prefix) :])
        assert probe.nonce == body.nonce and probe.job.operation_lease == sampled_lease(carrier.launch, 0)
        with pytest.raises(StateError):
            body.finish()
        actual = sampled_lease(carrier.launch, 10**18)
        tail = body.freeze_tail(actual)
        assert tail == encode_tail(actual)
        attempt = body.finish()
        try:
            assert attempt.invocation == invocation and isinstance(attempt.io.input, FiniteInput)
            request = decode_request(attempt.io.input.data[len(FIXED_BUNDLE.prefix) :])
            assert request.nonce == body.nonce and request.job.operation_lease == actual
            assert body.digest == hashlib.sha256(b"".join(body.parts) + tail).digest()
        finally:
            attempt.discard()
    finally:
        body.clear()


@pytest.mark.parametrize("size", [10_000, 200_000])
def test_proxmox_large_body_selects_staged_without_changing_limits(size: int) -> None:
    carrier = Carrier(proxmox=True)
    body = prepare(carrier, size=size)
    try:
        assert body.staged and carrier.calls == 0 and len(carrier.validations) == 2
        invocation, io = carrier.validations[-1]
        assert len(ProxmoxCarrier._request_body(invocation, io)) <= 65536
        assert isinstance(io.input, FiniteInput)
        probe = decode_staged_request(io.input.data[io.input.data.index(b"{") :])
        assert probe.reference._ownership._parent == _Identity(2**64 - 1, 2**64 - 1)
        assert probe.reference._ownership._length == body.expected_length
        body.freeze_tail(sampled_lease(carrier.launch, 12345))
        with pytest.raises(ValidationError):
            body.finish(probe)
        assert carrier.calls == 0
    finally:
        body.clear()


def test_actual_staged_control_uses_original_nonce_and_is_validated_again() -> None:
    carrier = Carrier(staged=True, proxmox=True)
    body = prepare(carrier, size=10_000)
    try:
        body.freeze_tail(sampled_lease(carrier.launch, 12345))
        control = activation(body)
        attempt = body.finish(control)
        try:
            assert len(carrier.validations) == 3 and carrier.calls == 0
            assert isinstance(attempt.io.input, FiniteInput)
            actual = decode_staged_request(attempt.io.input.data[attempt.io.input.data.index(b"{") :])
            assert actual == control
            assert len(ProxmoxCarrier._request_body(attempt.invocation, attempt.io)) <= 65536
        finally:
            attempt.discard()
    finally:
        body.clear()


def test_structural_staged_refusal_precedes_all_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    carrier = Carrier(staged=True)
    original = carrier.validate

    def refuse(invocation, *, io):
        if carrier.validations:
            raise ValidationError("staged envelope refused")
        original(invocation, io=io)

    monkeypatch.setattr(carrier, "validate", refuse)
    with pytest.raises(ValidationError):
        prepare(carrier)
    assert carrier.calls == 0


@pytest.mark.parametrize("sample", [0, 1, 999999999, 10**18, MAX_CLOCK_NS - WINDOW_NS])
def test_real_op_tail_preserves_probe_complete_json_length(sample: int) -> None:
    carrier = Carrier()
    body = prepare(carrier)
    try:
        invocation, io = carrier.validations[0]
        probe_length = len(ProxmoxCarrier._request_body(invocation, io))
        body.freeze_tail(sampled_lease(carrier.launch, sample))
        attempt = body.finish()
        try:
            assert len(ProxmoxCarrier._request_body(attempt.invocation, attempt.io)) == probe_length
        finally:
            attempt.discard()
    finally:
        body.clear()


@pytest.mark.parametrize("operation", [False, True])
def test_body_encoded_once_and_hash_state_only_receives_actual_tail(
    monkeypatch: pytest.MonkeyPatch, operation: bool
) -> None:
    calls = []

    def counted(request):
        calls.append(request.nonce)
        return encode_body(request)

    monkeypatch.setattr(preparation, "encode_body", counted)
    carrier = Carrier(staged=True)
    body = prepare(carrier, operation=operation, size=200_000)
    state = body._hash
    updates = []

    class TailHash:
        def update(self, data):
            updates.append(data)
            state.update(data)

        def digest(self):
            return state.digest()

    monkeypatch.setattr(body, "_hash", TailHash())
    try:
        lease = sampled_lease(carrier.launch, 1234) if operation else None
        tail = body.freeze_tail(lease)
        attempt = body.finish(activation(body))
        attempt.discard()
        assert calls == [body.nonce] and updates == [tail] and len(tail) == MAX_LEASE_BYTES
    finally:
        body.clear()


@pytest.mark.parametrize("variant", ["missing", "wrong-run", "wrong-digest", "wrong-boot", "independent"])
def test_tail_refusal_is_one_shot_and_cannot_create_attempt(variant: str) -> None:
    carrier = Carrier()
    body = prepare(carrier, operation=variant != "independent")
    _, other_job = facts()
    original_lease = sampled_lease(other_job.launch, 1)
    lease: OperationLease | None = original_lease
    if variant == "missing":
        lease = None
    elif variant == "wrong-run":
        lease = replace(original_lease, run_id="e" * 32)
    elif variant == "wrong-digest":
        lease = replace(original_lease, receipt_sha256="e" * 64)
    elif variant == "wrong-boot":
        lease = replace(original_lease, boot_id="00000000-0000-4000-8000-000000000002")
    try:
        with pytest.raises(ValidationError):
            body.freeze_tail(lease)
        with pytest.raises(StateError):
            body.freeze_tail(None)
        with pytest.raises(StateError):
            body.finish()
        assert body.digest is None and carrier.calls == 0
    finally:
        body.clear()


@pytest.mark.parametrize("variant", ["nonce", "root", "length", "digest", "missing"])
def test_staged_finish_refuses_mismatched_actual_control(variant: str) -> None:
    carrier = Carrier(staged=True)
    body = prepare(carrier)
    body.freeze_tail(sampled_lease(carrier.launch, 1))
    original_control = activation(body)
    control: StagedHelperRequest | None = original_control
    if variant == "nonce":
        control = replace(original_control, nonce="e" * 32)
    elif variant == "root":
        root = replace(ROOT, groups=(0, 1))
        ownership = replace(
            original_control.reference._ownership, _context=ScratchReceiptContext(ScratchOperation.STAGE, root)
        )
        control = replace(original_control, identity=root, reference=ScratchReference(ownership))
    elif variant == "length":
        control = replace(
            original_control, reference=ScratchReference(replace(original_control.reference._ownership, _length=1))
        )
    elif variant == "digest":
        control = replace(original_control, sha256=b"e" * 32)
    else:
        control = None
    try:
        with pytest.raises(ValidationError):
            body.finish(control)
        with pytest.raises(StateError):
            body.finish(activation(body))
        assert carrier.calls == 0
    finally:
        body.clear()


def test_clear_releases_body_hash_and_endpoint_and_forbids_further_use() -> None:
    carrier = Carrier()
    reference = weakref.ref(carrier)
    body = prepare(carrier)
    del carrier
    assert reference() is not None
    body.clear()
    gc.collect()
    assert reference() is None and body.parts == () and body.digest is None and body._hash is None
    for action in (lambda: body.freeze_tail(None), body.finish):
        with pytest.raises(StateError):
            action()
    body.clear()


def test_actual_attempt_keeps_original_claim_binding_and_expiry(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    carrier, other = Carrier(), Carrier()
    deadline = Deadline.after(30)
    body = prepare(carrier, deadline=deadline)
    try:
        body.freeze_tail(sampled_lease(carrier.launch, 1))
        attempt = body.finish()
        body.clear()
        spec, _ = facts()
        record = ManagedRunRepository(database).reserve(spec, output_policy=POLICY, identity=RUN)
        for wrong_carrier, wrong_deadline in ((other, deadline), (carrier, Deadline(deadline.expires_at))):
            with pytest.raises(ValidationError):
                attempt.claim(record, wrong_carrier, wrong_deadline)
        attempt.verify(record, carrier, deadline)
        object.__setattr__(deadline, "expires_at", 0.0)
        with pytest.raises(ValidationError):
            attempt.claim(record, carrier, deadline)
        attempt.discard()
    finally:
        body.clear()
        database.close()


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("noise,code", [(False, 0), (True, 0), (False, 1)])
def test_actual_response_reducer_is_shared_with_existing_prepare(staged: bool, noise: bool, code: int) -> None:
    carrier = Carrier(staged=staged)
    body = prepare(carrier)
    try:
        lease = sampled_lease(carrier.launch, 1234)
        body.freeze_tail(lease)
        attempt = body.finish(activation(body) if staged else None)
        carrier.noise, carrier.code = noise, code
        result = exchange._exchange(carrier, attempt, attempt.deadline)
        assert result.observation is not None
        assert result.observation.state is (
            exchange.ManagedStartState.ACKNOWLEDGED if not noise and not code else exchange.ManagedStartState.INVALID
        )
        assert attempt.io is None and carrier.calls == 1
    finally:
        body.clear()


@pytest.mark.parametrize("variant", ["lease", "launch", "policy", "root", "runtime", "guest", "unbounded", "expired"])
def test_body_context_refusal_precedes_structural_probes(variant: str) -> None:
    carrier = Carrier()
    spec, request = facts()
    plan, runtime, guest, deadline = PLAN, RUNTIME, GUEST, Deadline.after(30)
    if variant == "lease":
        request = replace(request, operation_lease=sampled_lease(request.launch, 1))
    elif variant == "launch":
        request = replace(request, launch=b"invalid")
    elif variant == "policy":
        request = replace(request, capture_prefix_bytes=1)
    elif variant == "root":
        plan = IdentityPlan(replace(ROOT, euid=1), IdentityMode.DEMOTE)
    elif variant == "runtime":
        runtime = RuntimeSelection(RuntimeTargetOS.DARWIN)
    elif variant == "guest":
        guest = replace(GUEST, init_start_ticks=GUEST.init_start_ticks + 1)
    elif variant == "unbounded":
        deadline = Deadline.after(None)
    else:
        deadline = Deadline.after(0)
    with pytest.raises(ValidationError):
        prepare_managed_start_body(carrier, RUN, spec, POLICY, request, plan, deadline, runtime, guest)
    assert carrier.validations == [] and carrier.calls == 0


@pytest.mark.parametrize("frozen", [False, True])
def test_expired_original_budget_cannot_freeze_or_finish(frozen: bool) -> None:
    carrier = Carrier()
    deadline = Deadline.after(30)
    body = prepare(carrier, deadline=deadline)
    lease = sampled_lease(carrier.launch, 1)
    try:
        if frozen:
            body.freeze_tail(lease)
        object.__setattr__(deadline, "expires_at", 0.0)
        with pytest.raises(StateError):
            body.finish() if frozen else body.freeze_tail(lease)
        assert len(carrier.validations) == 1 and carrier.calls == 0
    finally:
        body.clear()


def test_finish_is_one_shot_and_inline_refuses_staged_control() -> None:
    carrier = Carrier()
    body = prepare(carrier)
    try:
        body.freeze_tail(sampled_lease(carrier.launch, 1))
        with pytest.raises(ValidationError):
            body.finish(activation(body))
        with pytest.raises(StateError):
            body.finish()
        assert len(carrier.validations) == 1 and carrier.calls == 0
    finally:
        body.clear()


@pytest.mark.parametrize("during_finish", [False, True])
def test_original_validation_controls_escape_unchanged(during_finish: bool) -> None:
    carrier = Carrier()
    interrupted = KeyboardInterrupt("validation interrupted")
    if not during_finish:
        carrier.control = interrupted
        with pytest.raises(KeyboardInterrupt) as caught:
            prepare(carrier)
        assert caught.value is interrupted and carrier.calls == 0
        return
    body = prepare(carrier)
    try:
        body.freeze_tail(sampled_lease(carrier.launch, 1))
        carrier.control = interrupted
        with pytest.raises(KeyboardInterrupt) as caught:
            body.finish()
        assert caught.value is interrupted and carrier.calls == 0
        with pytest.raises(StateError):
            body.finish()
        assert all(value is not interrupted for value in vars(body).values())
    finally:
        body.clear()
