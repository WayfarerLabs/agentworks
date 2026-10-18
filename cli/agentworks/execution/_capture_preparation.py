"""Passive admission of the complete capture control and retrieval envelopes."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._capture_control import CaptureCreation, CaptureStreamFact, encode_capture_fact, parse_capture_fact
from ._evidence_wire import Frame, FrameKind, encode_frame
from ._file_snapshot_exchange import _exchange
from ._file_snapshot_protocol import (
    MAX_SNAPSHOT_CHUNK_BYTES,
    FileSnapshotChunkRequest,
    FileSnapshotCleanupRequest,
    FileSnapshotReconcileRequest,
)
from ._inline_control import StreamEnd, StreamName, StreamRetention
from ._scratch import ReadyScratchReference, ScratchReference, _cleanup_debt
from ._scratch_receipt import ScratchOperation, ScratchOwnership, ScratchReceiptContext, _Identity
from .carrier import CarrierReport, Dispatch

if TYPE_CHECKING:
    from ._capture_obligation import CaptureObligation
    from .carrier import Carrier, CarrierIO, ChannelFeatures, Deadline, PreparedInvocation


class _ValidateOnly:
    """No dispatch authority: inspect the final exchange IO at its carrier boundary."""

    def __init__(self, carrier: Carrier) -> None:
        self._carrier = carrier

    @property
    def features(self) -> ChannelFeatures:
        return self._carrier.features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._carrier.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        return CarrierReport(Dispatch.NOT_SENT)


def validate_capture(carrier: Carrier, binding: CaptureObligation, limit: int, deadline: Deadline) -> None:
    """Prove fixed record limits and maximum-width complete helper carriers pre-effect."""
    maximum = _Identity(2**64 - 1, 2**64 - 1)
    nonce, identity = "f" * 32, binding.workload.expected
    probe = _ValidateOnly(carrier)
    for index, stream in enumerate(StreamName):
        token = binding.tokens[index]
        ready = ReadyScratchReference(
            ScratchReference(
                ScratchOwnership(
                    token,
                    ScratchReceiptContext(ScratchOperation.SNAPSHOT, identity),
                    maximum,
                    maximum,
                    maximum,
                    2**64 - 1,
                    limit,
                    maximum,
                )
            ),
            bytes(32),
            -(2**63),
            -(2**63),
        )
        end = StreamEnd(stream, limit, bytes(32).hex(), True, False, StreamRetention.CAPTURED)
        for fact in (
            CaptureStreamFact(end, CaptureCreation.POSSIBLE, ready),
            CaptureStreamFact(end, CaptureCreation.POSSIBLE),
        ):
            body = encode_capture_fact(fact)
            try:
                encode_frame(nonce, Frame(2**63 - 1, FrameKind.STREAM_END, body))
                parse_capture_fact(body, binding.tokens, identity, limit)
            except ValueError:
                raise ValidationError("Capture records exceed the fixed control admission") from None
        remaining = 1.7976931348623157e308
        # Read/control envelopes use independently maximum-width valid fields,
        # not a zero-length range whose shorter decimal width could hide a
        # late carrier overflow after publication.
        widest = replace(ready, _reference=ScratchReference(replace(ready._reference._ownership, _length=2**63 - 1)))
        requests = (
            FileSnapshotChunkRequest(
                nonce,
                token,
                widest,
                2**63 - 1 - MAX_SNAPSHOT_CHUNK_BYTES,
                MAX_SNAPSHOT_CHUNK_BYTES,
                identity,
                remaining,
            ),
            FileSnapshotReconcileRequest(nonce, token, identity, remaining),
            FileSnapshotCleanupRequest(nonce, token, _cleanup_debt(ready), identity, remaining),
        )
        for request in requests:
            _exchange(
                probe,
                request,
                binding.workload,
                deadline,
                binding.preparation.runtime,
                bootstrap=binding.preparation.bootstrap,
            )
