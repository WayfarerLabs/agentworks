"""Private two-gate host preparation for one Linux terminal handoff attempt."""

from __future__ import annotations

import base64
import secrets
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.resources import files
from typing import TYPE_CHECKING, NoReturn

from agentworks.errors import ValidationError
from agentworks.execution._process import SinkWriteError, try_write_to_sink
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

_HELPER_ENV = ("PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C")
_RUNTIME = "/usr/bin/python3"


def _build_fixed_source() -> str:
    encoded = base64.b64encode(files(__package__).joinpath("_terminal_guest.py").read_bytes()).decode("ascii")
    return (
        "import base64,sys,types\n"
        "m=types.ModuleType('_agw_terminal_guest');m.__file__='<agw-terminal-guest>'\n"
        "sys.modules[m.__name__]=m\n"
        f"exec(compile(base64.b64decode({encoded!r}),m.__file__,'exec'),m.__dict__)\n"
        "raise SystemExit(m.run(sys.argv[1]))\n"
    )


FIXED_SOURCE = _build_fixed_source()


class TerminalHandoffFailure(StrEnum):
    """Closed host-side failure categories without carrier or payload text."""

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
    PAYLOAD = "payload"
    DELIVERING = "delivering"
    INTERACTIVE = "interactive"
    HANDED_OFF = "handed_off"
    FAILED = "failed"


@dataclass
class _State:
    gate: _Gate = _Gate.PAYLOAD
    failure: TerminalHandoffFailure | None = None

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
        if self._state.gate in {_Gate.PAYLOAD, _Gate.INTERACTIVE}:
            return None
        if self._state.gate is _Gate.HANDED_OFF:
            return b""
        end = min(len(self._payload), self._offset + limit)
        chunk = self._payload[self._offset : end]
        self._offset = end
        if self._offset == len(self._payload):
            self._state.gate = _Gate.INTERACTIVE
        return chunk


class _ReadinessSink:
    """Suppress readiness/setup bytes, then borrow one presentation sink."""

    def __init__(self, state: _State, nonce: str, presentation: ByteSink) -> None:
        self._state = state
        self._prefix = READINESS_MAGIC + nonce.encode("ascii") + b":"
        self._presentation = presentation
        self._matched = 0
        self._awaiting_kind = False

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
) -> PreparedTerminalHandoff:
    """Build one no-staging terminal preparation for a Linux guest without dispatching it."""
    if not callable(getattr(presentation, "try_write", None)):
        raise ValidationError("Terminal handoff requires a trusted presentation sink")
    payload = _payload(argv, env, source)
    nonce = secrets.token_hex(16)
    state = _State()
    bootstrap = _BootstrapSource(state, payload)
    stdout = _ReadinessSink(state, nonce, presentation)
    invocation = PreparedInvocation(
        (
            "/usr/bin/env",
            "-i",
            *_HELPER_ENV,
            _RUNTIME,
            "-I",
            "-S",
            "-B",
            "-c",
            FIXED_SOURCE,
            nonce,
        )
    )
    return PreparedTerminalHandoff(invocation, bootstrap, stdout, nonce, state)
