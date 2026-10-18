"""Printable setup proof through strict decoding and actual Linux PTY handoff."""

from __future__ import annotations

import os
import struct

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _terminal_guest as guest
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution._terminal_handoff import (
    TerminalHandoffError,
    TerminalHandoffFailure,
    _payload,
    prepare_terminal_handoff,
)

from . import test_terminal_handoff as handoff

running_processes = handoff.running_processes


def _decode(monkeypatch: pytest.MonkeyPatch, data: bytes, *, limit: int = 7):
    offset = 0

    def read(fd: int, length: int) -> bytes:
        nonlocal offset
        assert fd == 71 and length > 0
        chunk = data[offset : offset + min(length, limit)]
        offset += len(chunk)
        return chunk

    monkeypatch.setattr(os, "read", read)
    result = guest._decode_payload(71)
    return result, offset


@pytest.mark.parametrize("case", ["upper", "lower", "mixed"])
@pytest.mark.parametrize("limit", [1, 13, guest.MAX_ENCODED_PAYLOAD_BYTES])
def test_hex_preserves_all_literal_octets_and_does_not_consume_keyboard(monkeypatch, case, limit):
    argv = (b"/bin/example", b"", b"A a\n\r\x01\xff", "café".encode())
    env = {b"KEY": b"V a\n\r\x01\xff", "clé".encode(): b""}
    source = bytes(range(256)) * 3
    wire = _payload(argv, env, source)
    assert all(byte in b"0123456789ABCDEF" for byte in wire)
    if case == "lower":
        wire = wire.lower()
    elif case == "mixed":
        wire = b"".join(bytes((byte,)).lower() if index % 2 else bytes((byte,)) for index, byte in enumerate(wire))
    (decoded_argv, decoded_env, decoded_source), consumed = _decode(monkeypatch, wire + b"keyboard\n", limit=limit)
    assert (decoded_argv, decoded_env, decoded_source) == (argv, env, source)
    assert consumed == len(wire)


def test_encoded_and_decoded_maximum_preserves_existing_acceptance(monkeypatch):
    argv = (b"/bin/true",)
    decoded_overhead = len(_payload(argv, {}, b"")) // 2
    source = b"\xff" * (32768 - decoded_overhead)
    wire = _payload(argv, {}, source)
    assert len(wire) == 65536
    assert len(bytes.fromhex(wire.decode("ascii"))) == 32768
    decoded, consumed = _decode(monkeypatch, wire, limit=997)
    assert decoded == (argv, {}, source) and consumed == len(wire)
    with pytest.raises(ValidationError):
        _payload(argv, {}, source + b"x")


@pytest.mark.parametrize("part", ["header", "body"])
@pytest.mark.parametrize("bad", [b"G", b"\x00", b"\xff", b" ", b"\n", b"\x1b"])
def test_nonhex_setup_bytes_fail_without_tolerant_terminal_normalization(monkeypatch, part, bad):
    wire = _payload((b"/bin/true",), {}, b"sensitive-source")
    offset = 0 if part == "header" else 2 * (len(guest.FRAME_MAGIC) + 4)
    with pytest.raises(guest._ProtocolError):
        _decode(monkeypatch, wire[:offset] + bad + wire[offset + 1 :])


@pytest.mark.parametrize("cut", [0, 1, 21, 22, 23, -1])
def test_truncated_encoded_header_or_body_never_decodes(monkeypatch, cut):
    wire = _payload((b"/bin/true",), {}, b"source")
    with pytest.raises(guest._ProtocolError):
        _decode(monkeypatch, wire[:cut])


@pytest.mark.parametrize("fault", ["wrong-magic", "oversized", "trailing-decoded", "bad-field"])
def test_decoded_frame_structure_remains_strict_and_bounded(monkeypatch, fault):
    decoded = bytes.fromhex(_payload((b"/bin/true",), {}, b"source").decode("ascii"))
    header = len(guest.FRAME_MAGIC) + 4
    if fault == "wrong-magic":
        decoded = b"X" + decoded[1:]
    elif fault == "oversized":
        decoded = guest.FRAME_MAGIC + struct.pack("!I", guest.MAX_PAYLOAD_BYTES - header + 1)
    elif fault == "trailing-decoded":
        body = decoded[header:] + b"x"
        decoded = guest.FRAME_MAGIC + struct.pack("!I", len(body)) + body
    else:
        # argv[0]'s declared field length exceeds the bounded decoded body.
        decoded = decoded[: header + 2] + struct.pack("!I", guest.MAX_PAYLOAD_BYTES) + decoded[header + 6 :]
    with pytest.raises(guest._ProtocolError):
        _decode(monkeypatch, decoded.hex().encode("ascii"))


@pytest.mark.parametrize("phase", [guest.PAYLOAD_READY, guest.INTERACTIVE_READY])
def test_readiness_is_printable_but_wrong_nonce_never_releases_input(phase):
    prepared, presentation = handoff._prepared()
    handoff._admit_runtime(prepared)
    wrong = guest._readiness("F" * 32 if prepared.nonce.upper() != "F" * 32 else "E" * 32, phase)
    assert all(0x20 <= byte <= 0x7E for byte in wrong)
    with pytest.raises(TerminalHandoffError) as caught:
        prepared.stdout.try_write(memoryview(wrong + handoff._marker(prepared, guest.PAYLOAD_READY)))
    assert caught.value.failure is TerminalHandoffFailure.PROTOCOL
    assert not prepared.handed_off and not presentation.data
    with pytest.raises(TerminalHandoffError):
        prepared.bootstrap.try_read(1)


@pytest.mark.parametrize("phase", [b"?", b"p", b"i", b"\x01"])
def test_nonce_bound_wrong_phase_does_not_admit_later_valid_record(phase):
    prepared, _ = handoff._prepared()
    handoff._admit_runtime(prepared)
    record = guest.READINESS_MAGIC + prepared.nonce.upper().encode("ascii") + b":" + phase
    with pytest.raises(TerminalHandoffError):
        prepared.stdout.try_write(memoryview(record + handoff._marker(prepared, guest.PAYLOAD_READY)))
    assert prepared.failure is TerminalHandoffFailure.PROTOCOL


@pytest.mark.parametrize("cut", [1, 14, 15, 30, 47])
def test_fragmented_readiness_never_releases_until_record_is_complete(cut):
    prepared, _ = handoff._prepared()
    handoff._admit_runtime(prepared)
    record = handoff._marker(prepared, guest.PAYLOAD_READY)
    assert prepared.stdout.try_write(memoryview(record[:cut])) == cut
    assert prepared.bootstrap.try_read(1) is None
    prepared.stdout.finish()
    assert prepared.failure is TerminalHandoffFailure.TRUNCATED


def test_actual_pty_payload_and_queued_keyboard_stay_separate(running_processes, monkeypatch):
    source = bytes(range(256)) + b"source-secret"
    secret = b"environment-secret-\xff"
    presentation = handoff.CollectSink(max_write=3)
    prepared = prepare_terminal_handoff(
        (b"/usr/bin/python3", b"-I", b"-S", b"-B", b"-c", handoff.CHILD_CODE, b"literal-\xff"),
        {b"SECRET": secret},
        source,
        presentation,
        runtime_selection=handoff._RUNTIME_SELECTION,
    )
    caller_master, caller_slave = os.openpty()
    try:
        os.write(caller_master, b"early-keyboard\n")
        running = handoff.TerminalProcess(prepared)
        running_processes.append(running)
        write = os.write
        writes = []

        def short_write(fd, data):
            if fd == running.master:
                writes.append(len(data))
                return write(fd, data[:3])
            return write(fd, data)

        monkeypatch.setattr(os, "write", short_write)
        assert prepared.bootstrap.try_read(1) is None
        running.wait_for_payload_gate()
        assert not prepared.handed_off and not presentation.data
        running.send_payload()
        assert prepared.bootstrap.try_read(1) is None
        running.wait_for_handoff()
        assert prepared.bootstrap.try_read(1) == b""
        # This fixture borrows the queued keyboard only at preparation EOF.
        # It does not model a native SSH or ConPTY relay.
        running._write_all(os.read(caller_slave, 1024))
        running.read_until_presented(presentation, b"INPUT:6561726c792d6b6579626f6172640a!")
        assert running.process.wait(timeout=5) == 0
        assert source not in running.raw_output and secret not in running.raw_output
        assert source.hex().upper().encode() not in running.raw_output
        assert secret.hex().upper().encode() not in running.raw_output
        assert any(length > 3 for length in writes) and len(writes) > 100
    finally:
        os.close(caller_slave)
        os.close(caller_master)


@pytest.mark.parametrize(
    "state",
    [
        RuntimePrerequisiteState.MISSING,
        RuntimePrerequisiteState.UNUSABLE,
        RuntimePrerequisiteState.UNSUPPORTED_VERSION,
        RuntimePrerequisiteState.MISSING_MODULES,
    ],
)
@pytest.mark.parametrize("uppercase", [False, True])
def test_closed_runtime_refusal_never_opens_printable_payload_gate(state, uppercase):
    prepared, presentation = handoff._prepared()
    token = "-" if state is RuntimePrerequisiteState.MISSING else "0"
    record = handoff._runtime_record(prepared, state, token=token, uppercase=uppercase, ending=b"\r\n")
    with pytest.raises(TerminalHandoffError) as caught:
        prepared.stdout.try_write(memoryview(record + handoff._marker(prepared, guest.PAYLOAD_READY)))
    assert caught.value.failure is TerminalHandoffFailure.PREREQUISITE
    prepared.stdout.finish()
    assert prepared.runtime_prerequisite.state is state and not presentation.data
    with pytest.raises(TerminalHandoffError):
        prepared.bootstrap.try_read(1)


def test_actual_pty_deadline_finalization_before_complete_payload_retains_no_handoff(running_processes):
    import termios

    prepared, presentation = handoff._prepared()
    running = handoff.TerminalProcess(prepared)
    running_processes.append(running)
    running.wait_for_payload_gate()
    running._write_all(guest.FRAME_MAGIC.hex().encode("ascii"))
    with pytest.raises(AssertionError):
        running.wait_for_handoff(timeout=0.01)
    prepared.stdout.finish()
    assert prepared.failure is TerminalHandoffFailure.TRUNCATED
    assert prepared.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert not prepared.handed_off and not presentation.data
    with pytest.raises(TerminalHandoffError):
        prepared.bootstrap.try_read(1)
    running.process.terminate()
    assert running.process.wait(timeout=5) != 0
    assert termios.tcgetattr(running.slave) == running.original_mode


@pytest.mark.parametrize("offset", [len(guest.READINESS_MAGIC) + 2, len(guest.READINESS_MAGIC) + 32])
def test_terminal_control_insertions_inside_readiness_fail_closed(offset):
    prepared, presentation = handoff._prepared()
    handoff._admit_runtime(prepared)
    record = handoff._marker(prepared, guest.PAYLOAD_READY)
    altered = record[:offset] + b"\x1b[1G" + record[offset:]
    with pytest.raises(TerminalHandoffError):
        prepared.stdout.try_write(memoryview(altered + record))
    assert prepared.failure is TerminalHandoffFailure.PROTOCOL
    assert not prepared.handed_off and not presentation.data
