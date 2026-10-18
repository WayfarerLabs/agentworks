"""One borrowed managed I/O attempt and its separate presentation evidence."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._borrowed_io import DiscardSink, PrefixedSource, PresentationCollector
from ._evidence_wire import Frame, FrameKind, FrameReader, WireError
from ._inline_control import ControlError, StreamEnd, StreamName, StreamRetention, parse_empty, parse_stream_end
from ._managed_job_request import ManagedLiveIO
from ._managed_live_bundle import ROOT_PROGRAM
from ._managed_live_protocol import ManagedLiveRequest, encode_envelope
from ._process import try_write_to_sink
from ._runtime_prerequisite import (
    HelperClosureExpectation,
    RuntimePrefixSink,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    _NumericGuestBootstrap,
    build_root_guest_bootstrap_argv,
)
from .carrier import (
    CarrierIO,
    Deadline,
    Dispatch,
    Failure,
    FiniteInput,
    LiveInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)

if TYPE_CHECKING:
    from ._fixed_helper_operation import BoundHelperCarrier
    from .carrier import ByteSink, ByteSource, ExitStatus


class ManagedLiveIssue(StrEnum):
    BINDING = "binding"
    CONTROL = "control"
    ORDER = "order"
    STREAM = "stream"
    POST_TERMINAL = "post_terminal"
    MISSING_TERMINAL = "missing_terminal"
    CARRIER = "carrier"


@dataclass(frozen=True, slots=True)
class ManagedLiveObservation:
    """Existing-job binding and host presentation, never application completion."""

    bound: bool
    stdout: StreamEnd | None
    stderr: StreamEnd | None
    trusted_terminal: bool
    error: ManagedLiveIssue | WireError | None


class _ManagedLiveObserver:
    """Consume managed relay grammar without external I/O in codec callbacks."""

    def __init__(
        self,
        expected_launch: bytes,
        live_io: ManagedLiveIO,
        *,
        stdout: ByteSink | None,
        stderr: ByteSink | None,
    ) -> None:
        self._expected_launch = expected_launch
        self._live_io = live_io
        self._bound = False
        self._terminal = False
        self._error: ManagedLiveIssue | None = None
        self._streams: dict[StreamName, StreamEnd] = {}
        self._sinks = {StreamName.STDOUT: stdout, StreamName.STDERR: stderr}
        self._pending: tuple[StreamName, bytes] | None = None
        self._offset = 0
        self._counts = {StreamName.STDOUT: 0, StreamName.STDERR: 0}
        self._hashes = {StreamName.STDOUT: hashlib.sha256(), StreamName.STDERR: hashlib.sha256()}

    def accept(self, frame: Frame) -> None:
        if self._error is not None:
            return
        if self._terminal:
            self._error = ManagedLiveIssue.POST_TERMINAL
            return
        try:
            self._accept(frame)
        except ControlError:
            self._error = ManagedLiveIssue.CONTROL

    def _accept(self, frame: Frame) -> None:
        if frame.kind is FrameKind.LAUNCHING:
            if self._bound:
                self._error = ManagedLiveIssue.ORDER
            elif frame.body != self._expected_launch:
                self._error = ManagedLiveIssue.BINDING
            else:
                self._bound = True
        elif not self._bound:
            self._error = ManagedLiveIssue.ORDER
        elif frame.kind in (FrameKind.STDOUT, FrameKind.STDERR):
            self._accept_data(frame)
        elif frame.kind is FrameKind.STREAM_END:
            self._accept_end(parse_stream_end(frame.body))
        elif frame.kind is FrameKind.FINISHED:
            parse_empty(frame.body)
            if self._pending is not None or (self._live_io.output and len(self._streams) != 2):
                self._error = ManagedLiveIssue.ORDER
            else:
                self._terminal = True
        else:
            self._error = ManagedLiveIssue.ORDER

    def _accept_data(self, frame: Frame) -> None:
        stream = StreamName.STDOUT if frame.kind is FrameKind.STDOUT else StreamName.STDERR
        if not self._live_io.output or stream in self._streams:
            self._error = ManagedLiveIssue.ORDER
        elif self._pending is not None or self._counts[stream] + len(frame.body) > 2**63 - 1:
            self._error = ManagedLiveIssue.STREAM
        else:
            self._counts[stream] += len(frame.body)
            self._hashes[stream].update(frame.body)
            if frame.body:
                self._pending = (stream, frame.body)

    def _accept_end(self, end: StreamEnd) -> None:
        if (
            not self._live_io.output
            or end.stream in self._streams
            or self._pending is not None
            or end.retention is not StreamRetention.DELIVERED
            or not end.complete
            or end.truncated
            or end.count != self._counts[end.stream]
            or end.sha256 != self._hashes[end.stream].hexdigest()
        ):
            self._error = ManagedLiveIssue.STREAM
        else:
            self._streams[end.stream] = end

    def drain(self) -> bool:
        """Offer at most one bounded decoded chunk outside the codec callback."""
        if self._pending is None:
            return True
        stream, data = self._pending
        sink = self._sinks[stream]
        assert sink is not None
        while self._offset < len(data):
            written = try_write_to_sink(sink, memoryview(data)[self._offset :])
            if written is None:
                return False
            self._offset += written
        self._pending = None
        self._offset = 0
        return True

    def finish(self, wire_error: WireError | None, *, carrier_stdout_complete: bool) -> ManagedLiveObservation:
        """Require complete framing and drained presentation without further I/O."""
        error: ManagedLiveIssue | WireError | None = self._error or wire_error
        if error is None:
            if self._pending is not None:
                error = ManagedLiveIssue.STREAM
            elif not carrier_stdout_complete:
                error = ManagedLiveIssue.CARRIER
            elif not self._terminal:
                error = ManagedLiveIssue.MISSING_TERMINAL
        return ManagedLiveObservation(
            bound=self._bound,
            stdout=self._streams.get(StreamName.STDOUT),
            stderr=self._streams.get(StreamName.STDERR),
            trusted_terminal=self._terminal and error is None,
            error=error,
        )

    def release(self) -> None:
        self._sinks.clear()
        self._pending = None
        self._offset = 0


@dataclass(repr=False)
class PreparedManagedLive:
    """One prepared relay, passively validated before borrowing its endpoints."""

    invocation: PreparedInvocation
    io: CarrierIO
    live_io: ManagedLiveIO
    closure_expectation: HelperClosureExpectation
    _runtime: RuntimePrefixSink
    _reader: FrameReader
    _observer: _ManagedLiveObserver
    _claimed: bool = field(default=False, init=False)

    def claim(self) -> None:
        if self._claimed:
            raise ValidationError("A managed live preparation cannot be dispatched more than once")
        self._claimed = True

    def clear(self) -> None:
        """Retire the attempt and drop borrowed endpoints and unfinished bytes."""
        self._claimed = True
        if isinstance(self.io.input, LiveInput) and isinstance(self.io.input.source, PrefixedSource):
            self.io.input.source.release()
        self.io = CarrierIO()
        self._reader.finish()
        self._runtime.clear()
        self._observer.release()


@dataclass(frozen=True, slots=True)
class ManagedLiveCandidate:
    """Carrier/runtime and presentation evidence, without raw application bytes."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: ManagedLiveObservation | None
    closure_expectation: HelperClosureExpectation


def prepare_managed_live(
    *,
    expected_launch: bytes,
    live_io: ManagedLiveIO,
    bootstrap: _NumericGuestBootstrap,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    source: ByteSource | None = None,
    stdout: ByteSink | None = None,
    stderr: ByteSink | None = None,
    sensitive: bool = False,
) -> PreparedManagedLive:
    """Prepare one root-bound VM relay without reading or writing endpoints."""
    if (
        type(live_io) is not ManagedLiveIO
        or type(bootstrap) is not _NumericGuestBootstrap
        or type(sensitive) is not bool
    ):
        raise ValidationError("Managed live preparation requires bound metadata, bootstrap and sensitivity")
    if (
        live_io.input != (source is not None)
        or live_io.output != (stdout is not None and stderr is not None)
        or (stdout is None) != (stderr is None)
    ):
        raise ValidationError("Managed live endpoints must match the protected directions")
    if type(deadline) is not Deadline:
        raise ValidationError("Managed live preparation requires a finite unexpired deadline")
    remaining = deadline.remaining()
    if remaining is None or not math.isfinite(remaining):
        raise ValidationError("Managed live preparation requires a finite unexpired deadline")
    numerator, denominator = remaining.as_integer_ratio()
    remaining_ns = numerator * 1_000_000_000 // denominator
    if not 0 < remaining_ns <= 2**63 - 1:
        raise ValidationError("Managed live preparation requires a positive bounded nanosecond budget")
    request = ManagedLiveRequest(
        live_io, bootstrap.root_entry.expected, bootstrap.guest, expected_launch, remaining_ns=remaining_ns
    )
    try:
        envelope = ROOT_PROGRAM.prefix + encode_envelope(request)
    except ValueError:
        raise ValidationError("Invalid managed live relay binding") from None
    argv, candidates, system_shim = build_root_guest_bootstrap_argv(
        bootstrap.root_entry,
        bootstrap.root_entry.expected,
        selection=runtime_selection,
        program=ROOT_PROGRAM,
        nonce=live_io.nonce,
        expected_guest=bootstrap.guest,
    )
    observer = _ManagedLiveObserver(expected_launch, live_io, stdout=stdout, stderr=stderr)
    reader = FrameReader(live_io.nonce, observer.accept)
    collector = PresentationCollector(reader, observer)
    runtime = RuntimePrefixSink(live_io.nonce, candidates, collector, system_shim)
    return PreparedManagedLive(
        invocation=PreparedInvocation(argv),
        io=CarrierIO(
            input=FiniteInput(envelope, sensitive=sensitive)
            if source is None
            else LiveInput(PrefixedSource(envelope, source), sensitive=sensitive),
            output=SinkOutput(runtime, DiscardSink(), require_live=True),
            sensitive=sensitive,
        ),
        live_io=live_io,
        closure_expectation=HelperClosureExpectation(live_io.nonce, candidates, system_shim, bootstrap.guest),
        _runtime=runtime,
        _reader=reader,
        _observer=observer,
    )


def execute_managed_live(
    carrier: BoundHelperCarrier,
    prepared: PreparedManagedLive,
    *,
    deadline: Deadline,
) -> ManagedLiveCandidate:
    """Observe exactly one relay call; caller retains helper and operation custody."""
    prepared.claim()
    try:
        report = carrier.execute(prepared.invocation, io=prepared.io, deadline=deadline)
        prepared._reader.finish()
        runtime = prepared._runtime.observation
        observation = None
        if runtime.state is RuntimePrerequisiteState.READY:
            delivered = (
                report.stdout.retention is Retention.DELIVERED and report.stderr.retention is Retention.DELIVERED
            )
            observation = prepared._observer.finish(
                prepared._reader.error, carrier_stdout_complete=delivered and report.stdout.complete
            )
        return ManagedLiveCandidate(
            dispatch=report.dispatch,
            carrier_completion=report.completion,
            carrier_local_status=report.local_status,
            carrier_failure=report.failure,
            runtime_prerequisite=runtime,
            observation=observation,
            closure_expectation=prepared.closure_expectation,
        )
    finally:
        prepared.clear()
