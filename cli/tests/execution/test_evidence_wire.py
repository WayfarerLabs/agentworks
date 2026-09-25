"""Behavioral checks for the bounded private evidence wire codec."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agentworks.execution._evidence_wire import Frame, FrameKind, FrameReader, WireError, encode_frame

NONCE = "0123456789abcdef0123456789abcdef"
OTHER_NONCE = "fedcba9876543210fedcba9876543210"
WIRE_PATH = Path(__file__).parents[2] / "agentworks" / "execution" / "_evidence_wire.py"
PYTHON_311 = Path("/usr/bin/python3.11")


def _feed(reader: FrameReader, data: bytes) -> list[int]:
    remaining = memoryview(data)
    writes = []
    while remaining:
        written = reader.try_write(remaining)
        assert 0 < written <= len(remaining)
        writes.append(written)
        remaining = remaining[written:]
    return writes


def _record(
    *,
    nonce: str = NONCE,
    sequence: bytes = b"0",
    kind: bytes = b"STDOUT",
    length: bytes = b"1",
    body: bytes = b"eA==",
) -> bytes:
    return b" ".join((b"AGWE1", nonce.encode("ascii"), sequence, kind, length, body)) + b"\n"


def test_encoder_is_canonical_and_frame_is_immutable_and_secret_safe() -> None:
    body = b"frame-repr-canary\x00\xff"
    frame = Frame(7, FrameKind.STDERR, body)

    assert (
        encode_frame(NONCE, frame)
        == b"AGWE1 0123456789abcdef0123456789abcdef 7 STDERR 19 ZnJhbWUtcmVwci1jYW5hcnkA/w==\n"
    )
    assert body.decode(errors="ignore") not in repr(frame)
    with pytest.raises(FrozenInstanceError):
        frame.sequence = 8  # type: ignore[misc]


@pytest.mark.parametrize("kind", list(FrameKind))
def test_all_frame_kinds_are_grammar_not_application_order(kind: FrameKind) -> None:
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)

    _feed(reader, encode_frame(NONCE, Frame(0, kind, b"")))
    _feed(reader, encode_frame(NONCE, Frame(1, FrameKind.STDOUT, b"after-kind")))
    reader.finish()

    assert frames == [Frame(0, kind, b""), Frame(1, FrameKind.STDOUT, b"after-kind")]
    assert reader.error is None


def test_every_record_split_boundary_roundtrips() -> None:
    record = encode_frame(NONCE, Frame(0, FrameKind.STDOUT, b"split-boundary"))
    for boundary in range(len(record) + 1):
        frames: list[Frame] = []
        reader = FrameReader(NONCE, frames.append)

        _feed(reader, record[:boundary])
        _feed(reader, record[boundary:])
        reader.finish()

        assert frames == [Frame(0, FrameKind.STDOUT, b"split-boundary")]
        assert reader.error is None


def test_byte_at_a_time_roundtrips_every_binary_value_at_body_limit() -> None:
    body = bytes(range(256)) * 16
    record = encode_frame(NONCE, Frame(0, FrameKind.STDOUT, body))
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)

    for byte in record:
        assert reader.try_write(memoryview(bytes((byte,)))) == 1
    reader.finish()

    assert frames == [Frame(0, FrameKind.STDOUT, body)]
    assert reader.error is None


def test_noise_and_other_nonces_are_dropped_without_preventing_short_writes() -> None:
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)
    noise_canary = b"unretained-hook-noise\x00\xff" * 50_000
    other = encode_frame(OTHER_NONCE, Frame(93, FrameKind.FAILED, b"other-nonce-canary"))
    own = encode_frame(NONCE, Frame(0, FrameKind.WAITED, b"owned"))
    stream = noise_canary + b"\n" + other + b"unterminated-hook-text\n" + own

    writes = _feed(reader, stream)
    reader.finish()

    assert len(writes) > 1
    assert any(written < len(stream) for written in writes)
    assert frames == [Frame(0, FrameKind.WAITED, b"owned")]
    assert reader.error is None
    assert noise_canary[:20].decode() not in repr(reader)


def test_embedded_own_frame_is_noise_but_next_line_frame_is_accepted_across_splits() -> None:
    embedded = encode_frame(NONCE, Frame(0, FrameKind.STDOUT, b"embedded-canary"))
    separated = encode_frame(NONCE, Frame(0, FrameKind.STDOUT, b"accepted"))
    stream = b"diagnostic-prefix" + embedded + separated

    for boundary in range(len(stream) + 1):
        frames: list[Frame] = []
        reader = FrameReader(NONCE, frames.append)

        _feed(reader, stream[:boundary])
        _feed(reader, stream[boundary:])
        reader.finish()

        assert frames == [Frame(0, FrameKind.STDOUT, b"accepted")]
        assert reader.error is None
        assert "embedded-canary" not in repr(reader)


@pytest.mark.parametrize(
    "record",
    [
        _record(sequence=b"00"),
        _record(kind=b"UNKNOWN"),
        _record(kind=b"\xff"),
        _record(length=b"01"),
        _record(length=b"2"),
        _record(length=b"4097"),
        _record(body=b"AB=="),
        _record(body=b"eA="),
        _record(body=b"eA==", length=b"99999999999999999999"),
        _record()[:-1] + b" extra\n",
        b"AGWE1 " + NONCE.encode("ascii") + b"X 0 STDOUT 1 eA==\n",
    ],
)
def test_malformed_own_nonce_records_latch_a_safe_error(record: bytes) -> None:
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)

    _feed(reader, record)
    reader.finish()

    assert frames == []
    assert reader.error is WireError.MALFORMED
    assert "eA" not in repr(reader)


@pytest.mark.parametrize("bad_sequence", [b"0", b"2", str(2**63).encode("ascii")])
def test_duplicate_gapped_and_out_of_range_sequences_close_reader(bad_sequence: bytes) -> None:
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)

    _feed(reader, encode_frame(NONCE, Frame(0, FrameKind.LAUNCHING, b"")))
    _feed(reader, _record(sequence=bad_sequence))

    expected = WireError.MALFORMED if bad_sequence == str(2**63).encode("ascii") else WireError.SEQUENCE
    assert frames == [Frame(0, FrameKind.LAUNCHING, b"")]
    assert reader.error is expected


def test_oversized_own_record_closes_at_the_encoded_bound_and_drains() -> None:
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)
    canary = b"oversized-wire-canary"
    oversized = b"AGWE1 " + NONCE.encode("ascii") + b" 0 STDOUT 4096 " + canary * 500

    _feed(reader, oversized)
    first_error = reader.error
    _feed(reader, encode_frame(NONCE, Frame(0, FrameKind.FINISHED, b"")))
    reader.finish()

    assert first_error is WireError.OVERSIZED
    assert reader.error is first_error
    assert frames == []
    assert canary.decode() not in repr(reader)


def test_malformed_record_consumes_and_drains_the_rest_of_the_write() -> None:
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)
    malformed = _record(body=b"not-base64")
    valid = encode_frame(NONCE, Frame(0, FrameKind.FINISHED, b""))

    combined = malformed + valid
    assert reader.try_write(memoryview(combined)) == len(combined)
    _feed(reader, valid)
    reader.finish()

    assert reader.error is WireError.MALFORMED
    assert frames == []


def test_finish_latches_truncated_identified_record_and_stays_closed() -> None:
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)
    partial = encode_frame(NONCE, Frame(0, FrameKind.STDOUT, b"partial-canary"))[:-1]

    _feed(reader, partial)
    reader.finish()
    first_error = reader.error
    reader.finish()
    _feed(reader, b"\n" + encode_frame(NONCE, Frame(0, FrameKind.FINISHED, b"")))

    assert first_error is WireError.TRUNCATED
    assert reader.error is first_error
    assert frames == []
    assert b"partial-canary".decode() not in repr(reader)


def test_callback_exception_propagates_unchanged_after_reader_closes() -> None:
    class CallbackFailure(Exception):
        pass

    failure = CallbackFailure("trusted-callback-failure")
    calls: list[Frame] = []

    def fail(frame: Frame) -> None:
        calls.append(frame)
        raise failure

    reader = FrameReader(NONCE, fail)
    record = encode_frame(NONCE, Frame(0, FrameKind.STDOUT, b"callback-body-canary"))

    with pytest.raises(CallbackFailure) as caught:
        _feed(reader, record)
    assert caught.value is failure
    assert reader.error is WireError.CALLBACK
    _feed(reader, encode_frame(NONCE, Frame(1, FrameKind.STDERR, b"not-called")))
    assert calls == [Frame(0, FrameKind.STDOUT, b"callback-body-canary")]


@pytest.mark.parametrize(
    ("nonce", "frame"),
    [
        ("A" * 32, Frame(0, FrameKind.STDOUT, b"")),
        (NONCE, Frame(-1, FrameKind.STDOUT, b"")),
        (NONCE, Frame(2**63, FrameKind.STDOUT, b"")),
        (NONCE, Frame(0, FrameKind.STDOUT, b"x" * 4097)),
    ],
)
def test_encoder_rejects_invalid_configuration_without_echoing_values(nonce: str, frame: Frame) -> None:
    with pytest.raises(ValueError) as caught:
        encode_frame(nonce, frame)

    assert nonce not in str(caught.value)
    assert "x" * 32 not in str(caught.value)


@pytest.fixture(scope="module", params=[Path(sys.executable), PYTHON_311], ids=["current", "distribution-3.11"])
def interpreter(request: pytest.FixtureRequest) -> Path:
    python: Path = request.param
    if python == PYTHON_311 and not python.is_file():
        pytest.skip("This host has no /usr/bin/python3.11 compatibility interpreter")
    version = subprocess.run(
        [str(python), "-I", "-S", "-B", "-c", "import sys; print(*sys.version_info[:2])"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=5,
        text=True,
    )
    if python == PYTHON_311:
        assert version.stdout.strip() == "3 11"
    return python


@pytest.mark.windows
def test_standalone_stdlib_codec_roundtrips_all_binary_values(interpreter: Path) -> None:
    script = r"""
import importlib.util
import json
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("_standalone_evidence_wire", path)
assert spec is not None and spec.loader is not None
wire = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wire
spec.loader.exec_module(wire)
assert "agentworks" not in sys.modules

body = bytes(range(256)) * 16
frames = []
reader = wire.FrameReader("0123456789abcdef0123456789abcdef", frames.append)
record = wire.encode_frame(
    "0123456789abcdef0123456789abcdef",
    wire.Frame(0, wire.FrameKind.STDOUT, body),
)
for byte in record:
    assert reader.try_write(memoryview(bytes((byte,)))) == 1
reader.finish()
print(json.dumps({
    "agentworks_loaded": "agentworks" in sys.modules,
    "body": frames[0].body.hex(),
    "error": reader.error,
    "kind": frames[0].kind,
    "sequence": frames[0].sequence,
}))
"""
    completed = subprocess.run(
        [str(interpreter), "-I", "-S", "-B", "-c", script, str(WIRE_PATH)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=10,
    )
    observed = json.loads(completed.stdout)

    assert completed.stderr == b""
    assert observed == {
        "agentworks_loaded": False,
        "body": (bytes(range(256)) * 16).hex(),
        "error": None,
        "kind": "STDOUT",
        "sequence": 0,
    }
