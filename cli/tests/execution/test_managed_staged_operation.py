"""Caller-thread launch composition through real size limits and scripted helpers."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _managed_start_preparation as preparation_module
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_stage_protocol import FileStageChunkRequest, FileStageOperation
from agentworks.execution._managed_lease_wire import MAX_LEASE_BYTES
from agentworks.execution._managed_start_bundle import FIXED_BUNDLE
from agentworks.execution._managed_start_protocol import ManagedStartRequest, decode_request
from agentworks.execution._staged_helper_bundle import build_staged_helper_program
from agentworks.execution._staged_helper_protocol import decode_request as decode_activation
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers.proxmox import ProxmoxCarrier

from .test_managed_lease_exchange import ScriptedCarrier, _success
from .test_managed_operation_run import body_for, make_run
from .test_managed_operation_run import bound as bound
from .test_managed_start_operation import _records
from .test_prepared_helper_stage import Carrier as StageCarrier

# Real SQLite handles and host-side retained state must also work on Windows.
pytestmark = pytest.mark.windows


class StagedStartCarrier(StageCarrier):
    """Model stage storage while applying the actual bounded Proxmox encoder."""

    def __init__(self, owner) -> None:
        super().__init__(owner)
        # Keep fixed fixture packaging outside the timed helper exchanges.
        self.activation_prefix = build_staged_helper_program(FIXED_BUNDLE).prefix
        self.data = bytearray()
        self.events: list[str] = []
        self.activated: ManagedStartRequest | None = None
        self.bodies: list[bytes] = []
        self.activation_error: BaseException | None = None

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        ProxmoxCarrier._request_body(invocation, io)

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        self.bodies.append(ProxmoxCarrier._request_body(invocation, io))
        assert isinstance(io.input, FiniteInput)
        prefix = self.activation_prefix
        if not io.input.data.startswith(prefix):
            report = super().execute(invocation, io=io, deadline=deadline, custody=custody)
            stage_request = self.requests[-1]
            if isinstance(stage_request, FileStageChunkRequest):
                assert stage_request.offset == len(self.data)
                self.data.extend(stage_request.data)
            self.events.append(stage_request.operation.value)
            return report
        self.events.append("activate")
        if self.activation_error is not None:
            raise self.activation_error
        control = decode_activation(io.input.data[len(prefix) :])
        assert len(self.data) == control.reference._ownership._length
        assert hashlib.sha256(self.data).digest() == control.sha256
        request = decode_request(bytes(self.data[len(FIXED_BUNDLE.prefix) :]))
        assert request.nonce == control.nonce
        self.activated = request
        assert isinstance(io.output, SinkOutput)
        io.output.stdout.try_write(
            memoryview(f"AGW_RUNTIME_1:{request.nonce}:ready:0\n".encode() + _records(request, receipt=True))
        )
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(0), stdout=output, stderr=output)


def test_bulk_staging_precedes_real_lease_and_small_activation_fits_proxmox(bound) -> None:
    _, owner, receipt = bound
    carrier, clock = StagedStartCarrier(owner), ScriptedCarrier()
    run, _, _, _ = make_run(bound, carrier, clock)
    body = body_for(receipt)
    body = replace(body, request=replace(body.request, stdin=b"x" * 10_000))
    sampled_lengths: list[int] = []

    def sample(request):
        carrier.events.append("clock")
        sampled_lengths.append(len(carrier.data))
        assert run._stage is not None
        assert len(carrier.data) == run._stage.expected_length - MAX_LEASE_BYTES
        return _success(request)

    clock.response = sample
    try:
        outcome = run.start(body, Deadline.after(30))
        assert outcome is not None and run.acknowledged
        assert carrier.activated is not None and carrier.activated.job.stdin == b"x" * 10_000
        assert run.keeper.initial is not None
        assert carrier.activated.job.operation_lease == run.keeper.initial.lease
        assert len(sampled_lengths) == 1
        sample_at = carrier.events.index("clock")
        assert carrier.events[sample_at + 1 :] == [FileStageOperation.CHUNK.value, "activate"]
        assert all(len(data) <= 65_536 for data in carrier.bodies)
        assert run._stage is not None and run._stage._activation_settled
        assert run.keeper.drain(Deadline.after(2)).drained
        run._finish_preparation_cleanup(Deadline.after(2))
        assert run._stage.cleaned and not run._stage.cleanup_resolution_uncertain
    finally:
        assert run.keeper.drain(Deadline.after(2)).drained


def test_final_preparation_refusal_cleans_stage_without_activation_or_fake_delivery(bound, monkeypatch) -> None:
    _, owner, receipt = bound
    carrier, clock = StagedStartCarrier(owner), ScriptedCarrier()
    run, _, _, _ = make_run(bound, carrier, clock)
    body = body_for(receipt)
    body = replace(body, request=replace(body.request, stdin=b"x" * 10_000))

    def refuse(self, control=None):
        assert control is not None
        raise ValidationError("actual activation refused")

    monkeypatch.setattr(preparation_module.ManagedStartPreparation, "finish", refuse)
    try:
        with pytest.raises(ValidationError):
            run.start(body, Deadline.after(30))
        assert "activate" not in carrier.events and run._start_custody is None
        assert run._stage is not None and run._stage._activation_settled
        run._finish_preparation_cleanup(Deadline.after(2))
        assert run._stage.cleaned
    finally:
        assert run.keeper.drain(Deadline.after(2)).drained
