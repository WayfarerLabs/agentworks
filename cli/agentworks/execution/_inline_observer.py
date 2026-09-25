"""Trusted incremental consumer for private inline-helper evidence frames."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum

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
    retained: int
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

    def __init__(self, output_mode: OutputMode, capture_limit: int) -> None:
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
            or self._output_mode is not OutputMode.CAPTURE
            or stream in self._streams
            or self._wait is not None
            or self._failure is not None
        ):
            self._error = ObservationError.ORDER
            return
        data = self._data[stream]
        if len(data) + len(frame.body) > self._capture_limit:
            self._error = ObservationError.STREAM
            data.clear()
            return
        self._saw_data = True
        data.extend(frame.body)

    def _accept_stream_end(self, end: StreamEnd) -> None:
        expected_retention = {
            OutputMode.CAPTURE: StreamRetention.CAPTURED,
            OutputMode.DISCARD: StreamRetention.DISCARDED,
            OutputMode.SUPPRESS: StreamRetention.SUPPRESSED,
        }[self._output_mode]
        data = bytes(self._data[end.stream])
        invalid = (
            not self._launching
            or end.stream in self._streams
            or self._wait is not None
            or self._failure is not None
            or end.retention is not expected_retention
            or end.retained != len(data)
            or end.sha256 != hashlib.sha256(data).hexdigest()
            or (end.truncated and len(data) != self._capture_limit)
        )
        if invalid:
            self._error = ObservationError.STREAM
            return
        self._streams[end.stream] = StreamObservation(
            data=data,
            retained=end.retained,
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
