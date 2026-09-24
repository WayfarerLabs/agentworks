"""Host observation and local fixed-helper checks for VM guest identity."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.execution import _vm_guest_identity_guest as guest_helper
from agentworks.execution._runtime_prerequisite import (
    RuntimePrefixSink,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
)
from agentworks.execution._vm_guest_identity import (
    VMGuestIdentityObservationError,
    VMGuestIdentityObservationState,
    _BoundedResponseSink,
    _DiagnosticSink,
    observe_vm_guest_identity,
)
from agentworks.execution._vm_guest_identity_bundle import FIXED_SOURCE
from agentworks.execution._vm_guest_identity_guest import _GuestRefusal, _identity
from agentworks.execution._vm_guest_identity_protocol import (
    MAX_VM_GUEST_IDENTITY_MESSAGE_BYTES,
    VMGuestIdentity,
    VMGuestIdentityFailure,
    decode_vm_guest_identity_response,
    encode_vm_guest_identity_failure,
    encode_vm_guest_identity_success,
)
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    EndOfInput,
    ExitStatus,
    Failure,
    PreparedInvocation,
    Retention,
    SinkOutput,
)

NONCE_MARKER = "agentworks-runtime-prerequisite"
BOOT_ID = "12345678-1234-1234-1234-123456789abc"
MARKER = "a" * 32


def _selection() -> RuntimeSelection:
    return RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)


def _nonce(invocation: PreparedInvocation) -> str:
    position = invocation.argv.index(NONCE_MARKER)
    return invocation.argv[position + 1]


@dataclass
class TranscriptCarrier:
    transcript: bytes
    stderr: bytes = b""
    stdout_complete: bool = True
    stderr_complete: bool = True
    calls: int = 0
    io: CarrierIO | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        pass

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        del deadline
        self.calls += 1
        self.io = io
        assert isinstance(io.output, SinkOutput)
        nonce = _nonce(invocation)
        transcript = self.transcript.replace(b"0" * 32, nonce.encode())
        io.output.stdout.try_write(memoryview(f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode() + transcript))
        io.output.stderr.try_write(memoryview(self.stderr))
        return CarrierReport(
            Dispatch.SENT,
            ExitStatus(code=0),
            0,
            CapturedOutput(complete=self.stdout_complete, retention=Retention.DELIVERED),
            CapturedOutput(complete=self.stderr_complete, retention=Retention.DELIVERED),
        )


class RaisingCarrier(TranscriptCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        del invocation, deadline
        self.calls += 1
        self.io = io
        assert isinstance(io.output, SinkOutput)
        io.output.stdout.try_write(memoryview(b"private-response"))
        io.output.stderr.try_write(memoryview(b"private-diagnostic"))
        raise RuntimeError("carrier failed")


def _observe(carrier: TranscriptCarrier, deadline: Deadline | None = None):
    return observe_vm_guest_identity(
        carrier,
        runtime_selection=_selection(),
        deadline=deadline or Deadline.after(5),
    )


def _output(carrier: TranscriptCarrier) -> tuple[RuntimePrefixSink, _BoundedResponseSink, _DiagnosticSink]:
    assert carrier.io is not None
    assert isinstance(carrier.io.output, SinkOutput)
    assert isinstance(carrier.io.output.stdout, RuntimePrefixSink)
    assert isinstance(carrier.io.output.stdout.downstream, _BoundedResponseSink)
    assert isinstance(carrier.io.output.stderr, _DiagnosticSink)
    return carrier.io.output.stdout, carrier.io.output.stdout.downstream, carrier.io.output.stderr


def _guest_tree(root: Path) -> tuple[Path, Path]:
    marker_parent = root / "var" / "lib" / "agentworks"
    boot_parent = root / "proc" / "sys" / "kernel" / "random"
    marker_parent.mkdir(parents=True)
    boot_parent.mkdir(parents=True)
    marker = marker_parent / "instance-id"
    boot = boot_parent / "boot_id"
    marker.write_bytes((MARKER + "\n").encode())
    marker.chmod(0o444)
    boot.write_bytes((BOOT_ID + "\n").encode())
    for path in (root / "var", root / "var" / "lib", marker_parent):
        path.chmod(0o755)
    return marker, boot


def _make_marker_writable(marker: Path, _boot: Path) -> None:
    marker.chmod(0o644)


def _replace_marker_with_symlink(marker: Path, _boot: Path) -> None:
    target = marker.with_name("other")
    marker.rename(target)
    marker.symlink_to(target)


def _add_marker_hard_link(marker: Path, _boot: Path) -> None:
    marker.with_name("other").hardlink_to(marker)


def _make_marker_parent_writable(marker: Path, _boot: Path) -> None:
    marker.parent.chmod(0o775)


def _replace_marker_parent_with_symlink(marker: Path, _boot: Path) -> None:
    parent = marker.parent
    target = parent.with_name("agentworks-real")
    parent.rename(target)
    parent.symlink_to(target, target_is_directory=True)


def test_success_preserves_carrier_facts_and_closes_input() -> None:
    identity = VMGuestIdentity(MARKER, BOOT_ID)
    carrier = TranscriptCarrier(encode_vm_guest_identity_success("0" * 32, identity))
    result = _observe(carrier)
    assert carrier.calls == 1
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.observation is not None
    assert result.observation.state is VMGuestIdentityObservationState.RESOLVED
    assert result.observation.identity == identity
    assert result.dispatch is Dispatch.SENT
    assert carrier.io is not None
    assert isinstance(carrier.io.input, EndOfInput)
    _, response, _ = _output(carrier)
    assert response.data == bytearray()


def test_typed_refusal_is_not_invalid_response() -> None:
    carrier = TranscriptCarrier(encode_vm_guest_identity_failure("0" * 32, VMGuestIdentityFailure.MARKER_MISSING))
    result = _observe(carrier)
    assert result.observation is not None
    assert result.observation.state is VMGuestIdentityObservationState.REFUSED
    assert result.observation.failure is VMGuestIdentityFailure.MARKER_MISSING
    assert result.observation.error is None


@pytest.mark.parametrize(
    ("transcript", "stderr", "stdout_complete", "stderr_complete", "error", "state"),
    [
        (b"{}", b"", True, True, VMGuestIdentityObservationError.RESPONSE, VMGuestIdentityObservationState.INVALID),
        (b"", b"", True, True, VMGuestIdentityObservationError.MISSING, VMGuestIdentityObservationState.INCOMPLETE),
        (
            b"x" * (MAX_VM_GUEST_IDENTITY_MESSAGE_BYTES + 1),
            b"",
            True,
            True,
            VMGuestIdentityObservationError.OVERSIZED,
            VMGuestIdentityObservationState.INVALID,
        ),
        (b"{}", b"noise", True, True, VMGuestIdentityObservationError.STDERR, VMGuestIdentityObservationState.INVALID),
        (b"{}", b"", False, True, VMGuestIdentityObservationError.STREAMS, VMGuestIdentityObservationState.INCOMPLETE),
    ],
)
def test_malformed_streams_fail_closed(
    transcript: bytes,
    stderr: bytes,
    stdout_complete: bool,
    stderr_complete: bool,
    error: VMGuestIdentityObservationError,
    state: VMGuestIdentityObservationState,
) -> None:
    result = _observe(
        TranscriptCarrier(
            transcript,
            stderr=stderr,
            stdout_complete=stdout_complete,
            stderr_complete=stderr_complete,
        )
    )
    assert result.observation is not None
    assert result.observation.state is state
    assert result.observation.error is error


def test_preexpired_deadline_does_not_dispatch() -> None:
    carrier = TranscriptCarrier(b"")
    result = _observe(carrier, Deadline.after(0))
    assert carrier.calls == 0
    assert result.dispatch is Dispatch.NOT_SENT
    assert result.carrier_failure is Failure.DEADLINE
    assert result.observation is None


def test_carrier_exception_clears_private_buffers() -> None:
    carrier = RaisingCarrier(b"")
    with pytest.raises(RuntimeError, match="carrier failed"):
        _observe(carrier)
    _, response, stderr = _output(carrier)
    assert response.data == bytearray()
    assert not stderr.saw_data


@pytest.mark.skipif(sys.platform != "linux", reason="the fixed guest identity helper is Linux-only")
def test_production_guest_entry_encodes_identity_from_fixed_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guest_tree(tmp_path)

    def rooted_identity() -> VMGuestIdentity:
        return _identity(str(tmp_path), (os.geteuid(), os.getegid()))

    nonce = "0" * 32
    output: list[bytes] = []
    monkeypatch.setattr(guest_helper, "_identity", rooted_identity)
    monkeypatch.setattr(guest_helper, "_write_all", output.append)

    assert guest_helper.main(nonce) == 0
    assert len(output) == 1
    assert decode_vm_guest_identity_response(output[0], nonce).identity == VMGuestIdentity(MARKER, BOOT_ID)


@pytest.mark.skipif(sys.platform != "linux", reason="the fixed guest identity helper is Linux-only")
def test_fifo_marker_refusal_is_bounded(tmp_path: Path) -> None:
    marker, _ = _guest_tree(tmp_path)
    marker.unlink()
    os.mkfifo(marker, 0o444)

    source = f"""
from agentworks.execution._vm_guest_identity_guest import _GuestRefusal, _identity
try:
    _identity({str(tmp_path)!r}, ({os.geteuid()!r}, {os.getegid()!r}))
except _GuestRefusal as error:
    print(error.failure.value, end='')
else:
    raise SystemExit(3)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", source],
        check=True,
        capture_output=True,
        timeout=3,
    )

    assert result.stderr == b""
    assert result.stdout == VMGuestIdentityFailure.MARKER_UNSAFE.value.encode()


@pytest.mark.skipif(sys.platform != "linux", reason="the fixed guest identity helper is Linux-only")
@pytest.mark.parametrize(
    "make_unsafe",
    [
        _make_marker_writable,
        _replace_marker_with_symlink,
        _add_marker_hard_link,
        _make_marker_parent_writable,
        _replace_marker_parent_with_symlink,
    ],
    ids=(
        "writable-marker",
        "symlink-marker",
        "multiply-linked-marker",
        "writable-parent",
        "symlink-parent",
    ),
)
def test_guest_probe_refuses_unsafe_marker_leaf(
    tmp_path: Path,
    make_unsafe: Callable[[Path, Path], None],
) -> None:
    marker, boot = _guest_tree(tmp_path)
    make_unsafe(marker, boot)

    with pytest.raises(_GuestRefusal) as raised:
        _identity(str(tmp_path), (os.geteuid(), os.getegid()))

    assert raised.value.failure is VMGuestIdentityFailure.MARKER_UNSAFE


@pytest.mark.skipif(sys.platform != "linux", reason="the fixed guest identity helper is Linux-only")
@pytest.mark.parametrize(
    ("marker_bytes", "boot_bytes"),
    [
        (b"A" * 32 + b"\n", (BOOT_ID + "\n").encode()),
        ((MARKER + "\n").encode(), b"not-a-uuid\n"),
        ((MARKER + "\nextra").encode(), (BOOT_ID + "\n").encode()),
    ],
    ids=("uppercase-marker", "invalid-boot-id", "trailing-marker-data"),
)
def test_guest_probe_refuses_noncanonical_fixed_path_values(
    tmp_path: Path,
    marker_bytes: bytes,
    boot_bytes: bytes,
) -> None:
    marker, boot = _guest_tree(tmp_path)
    marker.chmod(0o644)
    marker.write_bytes(marker_bytes)
    marker.chmod(0o444)
    boot.write_bytes(boot_bytes)

    with pytest.raises(_GuestRefusal) as raised:
        _identity(str(tmp_path), (os.geteuid(), os.getegid()))

    assert raised.value.failure is VMGuestIdentityFailure.INVALID_IDENTITY


@pytest.mark.skipif(sys.platform != "linux", reason="the fixed guest identity helper is Linux-only")
def test_guest_probe_refuses_unexpected_path_owner(tmp_path: Path) -> None:
    _guest_tree(tmp_path)

    with pytest.raises(_GuestRefusal) as raised:
        _identity(str(tmp_path), (-1, -1))

    assert raised.value.failure is VMGuestIdentityFailure.MARKER_UNSAFE


@pytest.mark.skipif(sys.platform != "linux", reason="the fixed guest identity helper is Linux-only")
@pytest.mark.parametrize("mode", (0o555, 0o750))
def test_guest_probe_accepts_protected_restrictive_parent_modes(tmp_path: Path, mode: int) -> None:
    marker, _ = _guest_tree(tmp_path)
    marker.parent.chmod(mode)

    try:
        observed = _identity(str(tmp_path), (os.geteuid(), os.getegid()))
    finally:
        marker.parent.chmod(0o755)

    assert observed == VMGuestIdentity(MARKER, BOOT_ID)


@pytest.mark.skipif(sys.platform != "linux", reason="the fixed guest identity helper is Linux-only")
@pytest.mark.integration
def test_fixed_bundle_executes_without_an_installed_agentworks_package() -> None:
    nonce = "1" * 32
    result = subprocess.run(
        [sys.executable, "-I", "-c", FIXED_SOURCE, nonce],
        check=True,
        capture_output=True,
    )

    assert result.stderr == b""
    decoded = decode_vm_guest_identity_response(result.stdout, nonce)
    assert (decoded.identity is None) != (decoded.failure is None)
    assert decoded.failure is not VMGuestIdentityFailure.RUNTIME
