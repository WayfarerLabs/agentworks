"""Private two-gate host preparation for one Linux terminal handoff attempt."""

from __future__ import annotations

import secrets
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn

from agentworks.errors import ValidationError
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._process import SinkWriteError, try_write_to_sink
from agentworks.execution._runtime_prerequisite import (
    MAX_RUNTIME_RECORD_BYTES,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    build_runtime_helper_argv,
    decode_runtime_prerequisite_record,
)
from agentworks.execution._terminal_guest import (
    FRAME_MAGIC,
    INTERACTIVE_READY,
    MAX_ITEMS,
    MAX_PAYLOAD_BYTES,
    PAYLOAD_READY,
    READINESS_MAGIC,
)
from agentworks.execution.carrier import ByteSink, PreparedInvocation

if TYPE_CHECKING:
    from collections.abc import Mapping

FIXED_SOURCE = build_helper_modules("_agw_terminal", ("_terminal_guest",)) + (
    "raise SystemExit(sys.modules['_agw_terminal._terminal_guest'].run(sys.argv[1].upper()))\n"
)


class TerminalHandoffFailure(StrEnum):
    """Closed host-side failure categories without carrier or payload text."""

    PREREQUISITE = "prerequisite"
    PROTOCOL = "protocol"
    TRUNCATED = "truncated"
    SOURCE = "source"
    PRESENTATION = "presentation"


class TerminalHandoffError(RuntimeError):
    """A safe local endpoint failure for the carrier to classify."""

    def __init__(self, failure: TerminalHandoffFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class _Gate(StrEnum):
    PREREQUISITE = "prerequisite"
    PAYLOAD = "payload"
    DELIVERING = "delivering"
    INTERACTIVE = "interactive"
    HANDED_OFF = "handed_off"
    FAILED = "failed"


@dataclass
class _State:
    gate: _Gate = _Gate.PREREQUISITE
    failure: TerminalHandoffFailure | None = None
    runtime_prerequisite: RuntimePrerequisiteObservation = field(
        default_factory=lambda: RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None)
    )

    def fail(self, failure: TerminalHandoffFailure) -> NoReturn:
        if self.failure is None:
            self.failure = failure
        self.gate = _Gate.FAILED
        raise TerminalHandoffError(self.failure)


class _BootstrapSource:
    """Release one bounded frame only between the two readiness gates."""

    def __init__(self, state: _State, payload: bytes) -> None:
        self._state = state
        self._payload = payload
        self._offset = 0

    def try_read(self, limit: int) -> bytes | None:
        if type(limit) is not int or limit <= 0:
            self._state.fail(TerminalHandoffFailure.SOURCE)
        if self._state.gate is _Gate.FAILED:
            raise TerminalHandoffError(self._state.failure or TerminalHandoffFailure.PROTOCOL)
        if self._state.gate is _Gate.HANDED_OFF:
            return b""
        if self._state.gate is not _Gate.DELIVERING:
            return None
        end = min(len(self._payload), self._offset + limit)
        chunk = self._payload[self._offset : end]
        self._offset = end
        if self._offset == len(self._payload):
            self._state.gate = _Gate.INTERACTIVE
        return chunk


class _ReadinessSink:
    """Suppress readiness/setup bytes, then borrow one presentation sink."""

    def __init__(
        self,
        state: _State,
        nonce: str,
        candidates: tuple[str, ...],
        system_shim: str | None,
        presentation: ByteSink,
    ) -> None:
        self._state = state
        self._nonce = nonce
        runtime_prefix = b"AGW_RUNTIME_1:" + nonce.encode("ascii") + b":"
        uppercase_runtime_prefix = runtime_prefix.upper()
        self._runtime_prefixes = tuple(dict.fromkeys((runtime_prefix, uppercase_runtime_prefix)))
        self._runtime_match = bytearray()
        self._runtime_candidate: bytearray | None = None
        self._candidates = candidates
        self._system_shim = system_shim
        self._prefix = READINESS_MAGIC + nonce.upper().encode("ascii") + b":"
        self._presentation = presentation
        self._matched = 0
        self._awaiting_kind = False

    def _decode_runtime_record(self, record: bytes) -> RuntimePrerequisiteObservation:
        normalized = record[:-2] + b"\n" if record.endswith(b"\r\n") else record
        observation = decode_runtime_prerequisite_record(
            normalized,
            nonce=self._nonce,
            candidates=self._candidates,
            system_shim=self._system_shim,
        )
        if observation.state is not RuntimePrerequisiteState.UNKNOWN:
            return observation
        if normalized != normalized.upper():
            return observation
        canonical = b"AGW_RUNTIME_1:" + normalized[len(b"AGW_RUNTIME_1:") :].lower()
        return decode_runtime_prerequisite_record(
            canonical,
            nonce=self._nonce,
            candidates=self._candidates,
            system_shim=self._system_shim,
        )

    def _accept_runtime_byte(self, byte: int) -> None:
        if self._runtime_candidate is not None:
            self._runtime_candidate.append(byte)
            if len(self._runtime_candidate) > MAX_RUNTIME_RECORD_BYTES:
                self._runtime_candidate = None
                self._runtime_match.clear()
                self._state.fail(TerminalHandoffFailure.PROTOCOL)
            if byte != ord("\n"):
                return
            record = bytes(self._runtime_candidate)
            self._runtime_candidate = None
            observation = self._decode_runtime_record(record)
            if observation.state is RuntimePrerequisiteState.UNKNOWN:
                self._state.fail(TerminalHandoffFailure.PROTOCOL)
            self._state.runtime_prerequisite = observation
            if observation.state is not RuntimePrerequisiteState.READY:
                self._state.fail(TerminalHandoffFailure.PREREQUISITE)
            self._state.gate = _Gate.PAYLOAD
            return

        self._runtime_match.append(byte)
        while self._runtime_match and not any(
            prefix.startswith(self._runtime_match) for prefix in self._runtime_prefixes
        ):
            del self._runtime_match[0]
        if any(prefix == self._runtime_match for prefix in self._runtime_prefixes):
            self._runtime_candidate = bytearray(self._runtime_match)
            self._runtime_match.clear()

    def _accept_kind(self, kind: int) -> bool:
        gate = self._state.gate
        if gate is _Gate.PAYLOAD and kind == PAYLOAD_READY:
            self._state.gate = _Gate.DELIVERING
            return False
        if gate is _Gate.INTERACTIVE and kind == INTERACTIVE_READY:
            self._state.gate = _Gate.HANDED_OFF
            return True
        self._state.fail(TerminalHandoffFailure.PROTOCOL)

    def _write_presentation(self, data: memoryview) -> int | None:
        failed = False
        result: int | None = None
        try:
            result = try_write_to_sink(self._presentation, data)
        except SinkWriteError:
            failed = True
        if failed:
            self._state.fail(TerminalHandoffFailure.PRESENTATION)
        return result

    def try_write(self, data: memoryview) -> int | None:
        if not isinstance(data, memoryview):
            self._state.fail(TerminalHandoffFailure.PROTOCOL)
        if not data:
            self._state.fail(TerminalHandoffFailure.PROTOCOL)
        if self._state.gate is _Gate.FAILED:
            raise TerminalHandoffError(self._state.failure or TerminalHandoffFailure.PROTOCOL)
        if self._state.gate is _Gate.HANDED_OFF:
            return self._write_presentation(data)

        for index, byte in enumerate(data):
            if self._state.gate is _Gate.PREREQUISITE:
                self._accept_runtime_byte(byte)
                continue
            if self._awaiting_kind:
                self._awaiting_kind = False
                handed_off = self._accept_kind(byte)
                if handed_off:
                    consumed = index + 1
                    suffix = data[consumed:]
                    if not suffix:
                        return consumed
                    written = self._write_presentation(suffix)
                    return consumed if written is None else consumed + written
                continue

            if byte == self._prefix[self._matched]:
                self._matched += 1
                if self._matched == len(self._prefix):
                    self._matched = 0
                    self._awaiting_kind = True
            else:
                self._matched = 1 if byte == self._prefix[0] else 0
        return len(data)

    def finish(self) -> None:
        self._runtime_match.clear()
        self._runtime_candidate = None
        self._matched = 0
        self._awaiting_kind = False
        if self._state.gate not in {_Gate.HANDED_OFF, _Gate.FAILED}:
            self._state.failure = TerminalHandoffFailure.TRUNCATED
            self._state.gate = _Gate.FAILED


@dataclass
class PreparedTerminalHandoff:
    """Single-use preparation guard and its paired borrowed endpoints."""

    invocation: PreparedInvocation
    bootstrap: _BootstrapSource = field(repr=False)
    stdout: _ReadinessSink = field(repr=False)
    nonce: str
    _state: _State = field(repr=False)
    _claimed: bool = field(default=False, init=False, repr=False)

    def claim(self) -> None:
        """Reject reuse of this preparation object; dispatch remains caller-owned."""
        if self._claimed:
            raise ValidationError("A terminal handoff preparation cannot be reused")
        self._claimed = True

    @property
    def failure(self) -> TerminalHandoffFailure | None:
        return self._state.failure

    @property
    def runtime_prerequisite(self) -> RuntimePrerequisiteObservation:
        return self._state.runtime_prerequisite

    @property
    def handed_off(self) -> bool:
        return self._state.gate is _Gate.HANDED_OFF


def _field(value: bytes) -> bytes:
    return struct.pack("!I", len(value)) + value


def _payload(argv: tuple[bytes, ...], env: Mapping[bytes, bytes], source: bytes) -> bytes:
    if (
        type(argv) is not tuple
        or not 0 < len(argv) <= MAX_ITEMS
        or any(type(argument) is not bytes or b"\0" in argument for argument in argv)
        or not argv[0].startswith(b"/")
    ):
        raise ValidationError("Terminal handoff requires absolute literal byte argv")
    if type(source) is not bytes:
        raise ValidationError("Terminal handoff source must be bytes")

    failed = False
    entries: tuple[object, ...] = ()
    try:
        entries = tuple(env.items())
    except Exception:
        failed = True
    if failed or len(entries) > MAX_ITEMS:
        raise ValidationError("Terminal handoff environment must be a bounded byte mapping")
    environment: list[tuple[bytes, bytes]] = []
    seen: set[bytes] = set()
    for entry in entries:
        if type(entry) is not tuple or len(entry) != 2:
            raise ValidationError("Terminal handoff environment must be a bounded byte mapping")
        name, value = entry
        if (
            type(name) is not bytes
            or type(value) is not bytes
            or not name
            or b"=" in name
            or b"\0" in name
            or b"\0" in value
            or name in seen
        ):
            raise ValidationError("Terminal handoff environment contains an invalid byte entry")
        seen.add(name)
        environment.append((name, value))
    environment.sort()

    body = bytearray(struct.pack("!H", len(argv)))
    for argument in argv:
        body.extend(_field(argument))
    body.extend(struct.pack("!H", len(environment)))
    for name, value in environment:
        body.extend(_field(name))
        body.extend(_field(value))
    body.extend(_field(source))
    payload = FRAME_MAGIC + struct.pack("!I", len(body)) + body
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValidationError("Terminal handoff payload exceeds the 32768-byte candidate bound")
    return bytes(payload)


def prepare_terminal_handoff(
    argv: tuple[bytes, ...],
    env: Mapping[bytes, bytes],
    source: bytes,
    presentation: ByteSink,
    *,
    runtime_selection: RuntimeSelection,
) -> PreparedTerminalHandoff:
    """Build one no-staging terminal preparation for a Linux guest without dispatching it."""
    if not callable(getattr(presentation, "try_write", None)):
        raise ValidationError("Terminal handoff requires a trusted presentation sink")
    payload = _payload(argv, env, source)
    nonce = secrets.token_hex(16)
    fixed_argv, candidates, system_shim = build_runtime_helper_argv(
        selection=runtime_selection,
        fixed_source=FIXED_SOURCE,
        nonce=nonce,
    )
    state = _State()
    bootstrap = _BootstrapSource(state, payload)
    stdout = _ReadinessSink(state, nonce, candidates, system_shim, presentation)
    invocation = PreparedInvocation(fixed_argv)
    return PreparedTerminalHandoff(invocation, bootstrap, stdout, nonce, state)
