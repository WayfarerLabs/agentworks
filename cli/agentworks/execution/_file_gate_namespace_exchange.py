"""One finite, non-replayed root-fenced namespace helper exchange."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.execution._file_effect_gate_protocol import GateControlProtocolError, parse_gate_control_finished
from agentworks.execution._file_gate_namespace_bundle import ROOT_PROGRAM
from agentworks.execution._file_gate_namespace_protocol import (
    NamespaceRequest,
    NamespaceStatus,
    decode_namespace_status,
    encode_namespace_request,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind
from agentworks.execution._file_wire_reader import FileRecordReader
from agentworks.execution._runtime_prerequisite import (
    RuntimePrefixSink,
    RuntimePrerequisiteState,
    build_root_guest_bootstrap_argv,
)
from agentworks.execution.carrier import CarrierIO, ExitStatus, FiniteInput, PreparedInvocation, Retention, SinkOutput

if TYPE_CHECKING:
    from agentworks.execution._file_gate_namespace_obligation import NamespaceObligation
    from agentworks.execution._fixed_helper_operation import BoundHelperCarrier
    from agentworks.execution.carrier import Deadline, Dispatch


@dataclass(frozen=True, slots=True)
class NamespaceResult:
    dispatch: Dispatch
    completion: ExitStatus | None
    status: NamespaceStatus | None


class _Collector:
    def __init__(self) -> None:
        self.status: NamespaceStatus | None = None
        self.terminal = False
        self.invalid = False
        self.stderr = False

    def accept(self, record: FileRecord) -> None:
        try:
            if self.terminal or self.invalid:
                raise GateControlProtocolError
            if record.kind is FileRecordKind.RESULT and self.status is None:
                self.status = decode_namespace_status(record.body)
            elif record.kind is FileRecordKind.FINISHED and self.status is not None:
                parse_gate_control_finished(record.body)
                self.terminal = True
            else:
                raise GateControlProtocolError
        except GateControlProtocolError:
            self.invalid = True
            self.status = None

    def try_write(self, data: memoryview) -> int:
        self.stderr = self.stderr or bool(data)
        return len(data)


def exchange_namespace(carrier: BoundHelperCarrier, record: NamespaceObligation, deadline: Deadline) -> NamespaceResult:
    assert record.binding is not None
    bootstrap = record.bootstrap
    argv, candidates, shim = build_root_guest_bootstrap_argv(
        bootstrap.root_entry,
        bootstrap.root_entry.expected,
        selection=record.runtime_selection,
        program=ROOT_PROGRAM,
        nonce=record.nonce,
        expected_guest=bootstrap.guest,
    )
    remaining = deadline.remaining()
    assert remaining is not None
    manifest = encode_namespace_request(
        NamespaceRequest(record.nonce, record.uid, record.gid, bootstrap.root_entry.expected, record.binding, remaining)
    )
    collector = _Collector()
    reader = FileRecordReader(record.nonce, collector.accept)
    runtime = RuntimePrefixSink(record.nonce, candidates, reader, shim)
    io = CarrierIO(
        input=FiniteInput(ROOT_PROGRAM.prefix + manifest, sensitive=True),
        output=SinkOutput(runtime, collector, require_live=False),
        sensitive=True,
    )
    try:
        report = carrier.execute(PreparedInvocation(argv), io=io, deadline=deadline)
        reader.finish()
        status = collector.status
        if (
            runtime.observation.state is not RuntimePrerequisiteState.READY
            or report.failure is not None
            or report.completion != ExitStatus(code=0)
            or report.stdout.retention is not Retention.DELIVERED
            or report.stderr.retention is not Retention.DELIVERED
            or not report.stdout.complete
            or not report.stderr.complete
            or collector.stderr
            or collector.invalid
            or not collector.terminal
            or reader.error is not None
        ):
            status = None
        return NamespaceResult(report.dispatch, report.completion, status)
    finally:
        runtime.clear()
