"""Trusted incremental consumer for private inline-helper evidence frames."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .carrier import ByteSink

from ._evidence_wire import Frame, FrameKind, WireError
from ._inline_control import (
    ControlError,
    FailureFact,
    FailurePhase,
    StreamEnd,
    StreamName,
    StreamRetention,
    WaitFact,
    parse_empty,
    parse_failure,
    parse_stream_end,
    parse_wait,
)
from ._inline_request import OutputMode
from ._process import try_write_to_sink


class ObservationError(StrEnum):
    CONTROL = "control"
    ORDER = "order"
    STREAM = "stream"
    POST_TERMINAL = "post_terminal"
    MISSING_TERMINAL = "missing_terminal"
    CARRIER = "carrier"


@dataclass(frozen=True, slots=True)
class StreamObservation:
    data: bytes = field(repr=False)
    count: int
    sha256: str
    complete: bool
    truncated: bool
    retention: StreamRetention


@dataclass(frozen=True, slots=True)
class InlineObservation:
    launching: bool
    stdout: StreamObservation | None
    stderr: StreamObservation | None
    wait: WaitFact | None
    failure: FailureFact | None
    trusted_terminal: bool
    error: ObservationError | WireError | None


class InlineObserver:
    """Validate phase grammar without retaining raw carrier noise or diagnostics."""

    def __init__(
        self,
        output_mode: OutputMode,
        capture_limit: int,
        *,
        stdout: ByteSink | None = None,
        stderr: ByteSink | None = None,
    ) -> None:
        self._output_mode = output_mode
        self._capture_limit = capture_limit
        self._launching = False
        self._saw_data = False
        self._data = {StreamName.STDOUT: bytearray(), StreamName.STDERR: bytearray()}
        self._streams: dict[StreamName, StreamObservation] = {}
        self._wait: WaitFact | None = None
        self._failure: FailureFact | None = None
        self._terminal = False
        self._error: ObservationError | None = None
        self._sinks = {StreamName.STDOUT: stdout, StreamName.STDERR: stderr}
        self._pending: tuple[StreamName, bytes] | None = None
        self._offset = 0
        self._counts = {StreamName.STDOUT: 0, StreamName.STDERR: 0}
        self._hashes = {StreamName.STDOUT: hashlib.sha256(), StreamName.STDERR: hashlib.sha256()}

    def drain(self) -> bool:
        """Offer the bounded decoded chunk outside the trusted decoder callback."""
        if self._pending is None:
            return True
        stream, data = self._pending
        sink = self._sinks[stream]
        assert sink is not None
        while self._offset < len(data):
            remaining = memoryview(data)[self._offset :]
            written = try_write_to_sink(sink, remaining)
            if written is None:
                return False
            self._offset += written
        self._pending = None
        self._offset = 0
        return True

    def release(self) -> None:
        """End endpoint borrowing and discard undelivered presentation bytes."""
        self._sinks.clear()
        self._pending = None
        self._offset = 0

    def accept(self, frame: Frame) -> None:
        """Consume one codec-validated frame; schema failures stay inside the sink."""
        if self._error is not None:
            return
        if self._terminal:
            self._error = ObservationError.POST_TERMINAL
            return
        try:
            self._accept(frame)
        except ControlError:
            self._error = ObservationError.CONTROL

    def _accept(self, frame: Frame) -> None:
        if frame.kind in (FrameKind.STDOUT, FrameKind.STDERR):
            self._accept_data(frame)
            return
        if frame.kind is FrameKind.STARTED:
            self._error = ObservationError.ORDER
            return
        if frame.kind is FrameKind.LAUNCHING:
            parse_empty(frame.body)
            if self._launching or self._failure is not None or self._streams or self._wait is not None:
                self._error = ObservationError.ORDER
            else:
                self._launching = True
            return
        if frame.kind is FrameKind.STREAM_END:
            self._accept_stream_end(parse_stream_end(frame.body))
            return
        if frame.kind is FrameKind.WAITED:
            wait = parse_wait(frame.body)
            if not self._launching or len(self._streams) != 2 or self._wait is not None or self._failure is not None:
                self._error = ObservationError.ORDER
            else:
                self._wait = wait
            return
        if frame.kind is FrameKind.FAILED:
            self._accept_failure(parse_failure(frame.body))
            return
        if frame.kind is FrameKind.FINISHED:
            parse_empty(frame.body)
            if self._valid_finish():
                self._terminal = True
            else:
                self._error = ObservationError.ORDER
            return
        self._error = ObservationError.ORDER

    def _accept_data(self, frame: Frame) -> None:
        stream = StreamName.STDOUT if frame.kind is FrameKind.STDOUT else StreamName.STDERR
        if (
            not self._launching
            or self._output_mode not in {OutputMode.CAPTURE, OutputMode.DELIVER}
            or stream in self._streams
            or self._wait is not None
            or self._failure is not None
        ):
            self._error = ObservationError.ORDER
            return
        self._saw_data = True
        if self._output_mode is OutputMode.DELIVER:
            if self._pending is not None or self._counts[stream] + len(frame.body) > 2**63 - 1:
                self._error = ObservationError.STREAM
                return
            self._counts[stream] += len(frame.body)
            self._hashes[stream].update(frame.body)
            if frame.body:
                self._pending = (stream, frame.body)
            return
        data = self._data[stream]
        if len(data) + len(frame.body) > self._capture_limit:
            self._error = ObservationError.STREAM
            data.clear()
            return
        data.extend(frame.body)

    def _accept_stream_end(self, end: StreamEnd) -> None:
        expected_retention = {
            OutputMode.CAPTURE: StreamRetention.CAPTURED,
            OutputMode.DISCARD: StreamRetention.DISCARDED,
            OutputMode.SUPPRESS: StreamRetention.SUPPRESSED,
            OutputMode.DELIVER: StreamRetention.DELIVERED,
        }[self._output_mode]
        data = bytes(self._data[end.stream])
        count = self._counts[end.stream] if self._output_mode is OutputMode.DELIVER else len(data)
        digest = (
            self._hashes[end.stream].hexdigest()
            if self._output_mode is OutputMode.DELIVER
            else hashlib.sha256(data).hexdigest()
        )
        invalid = (
            not self._launching
            or end.stream in self._streams
            or self._wait is not None
            or self._failure is not None
            or end.retention is not expected_retention
            or self._pending is not None
            or end.count != count
            or end.sha256 != digest
            or (end.truncated and len(data) != self._capture_limit)
        )
        if invalid:
            self._error = ObservationError.STREAM
            return
        self._streams[end.stream] = StreamObservation(
            data=data,
            count=end.count,
            sha256=end.sha256,
            complete=end.complete,
            truncated=end.truncated,
            retention=end.retention,
        )

    def _accept_failure(self, failure: FailureFact) -> None:
        if self._failure is not None:
            self._error = ObservationError.ORDER
            return
        prelaunch = not self._launching and failure.phase in {
            FailurePhase.REQUEST,
            FailurePhase.IDENTITY,
            FailurePhase.PREPARE,
        }
        launch_failure = (
            self._launching
            and failure.phase is FailurePhase.LAUNCH
            and not self._saw_data
            and not self._streams
            and self._wait is None
        )
        post_wait = (
            self._launching
            and failure.phase in {FailurePhase.OBSERVE, FailurePhase.CLEANUP}
            and len(self._streams) == 2
            and self._wait is not None
        )
        if not (prelaunch or launch_failure or post_wait):
            self._error = ObservationError.ORDER
            return
        self._failure = failure

    def _valid_finish(self) -> bool:
        if not self._launching:
            return self._failure is not None
        if self._failure is not None and self._failure.phase is FailurePhase.LAUNCH:
            return not self._streams and self._wait is None
        return len(self._streams) == 2 and self._wait is not None

    def finish(self, wire_error: WireError | None, *, carrier_stdout_complete: bool) -> InlineObservation:
        """Close observation while preserving independently validated facts."""
        error: ObservationError | WireError | None = self._error or wire_error
        trusted_terminal = self._terminal and error is None
        if error is None and (not carrier_stdout_complete or not self._terminal):
            error = ObservationError.CARRIER if not carrier_stdout_complete else ObservationError.MISSING_TERMINAL
        return InlineObservation(
            launching=self._launching,
            stdout=self._streams.get(StreamName.STDOUT),
            stderr=self._streams.get(StreamName.STDERR),
            wait=self._wait,
            failure=self._failure,
            trusted_terminal=trusted_terminal,
            error=error,
        )
