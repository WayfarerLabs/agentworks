"""The stream filters and the classifier, which decide what a request's outcome was."""

from __future__ import annotations

import itertools

import pytest

from agentworks.execution.agw_launch import STDLIB_MISSING_STATUS, refusal_status
from agentworks.execution.envelope import StreamFilter, classify, marker_prefix
from agentworks.execution.results import (
    Completed,
    NotDelivered,
    NotDeliveredReason,
    Unknown,
    UnknownReason,
)

NONCE = "ab" * 16
PREFIX = marker_prefix(NONCE)
START = PREFIX + b"start\n"

# Payloads that stress the hold-back: bytes that begin the closing prefix, the
# prefix minus its last byte, and the lead byte on its own.
PAYLOADS = [
    b"",
    b"plain\n",
    b"no newline at the end",
    b"\x1e",
    PREFIX[:-1],
    PREFIX[: len(PREFIX) // 2],
    b"text" + PREFIX[:-1],
    b"\x1e\x1e" + PREFIX[:3],
    bytes(range(256)),
]


def guest_stream(payload: bytes, closing: bytes, *, before: bytes = b"", after: bytes = b"") -> bytes:
    return before + START + payload + PREFIX + closing + b"\n" + after


def run_filter(chunks: list[bytes]) -> tuple[bytes, StreamFilter]:
    stream = StreamFilter(NONCE)
    out = b"".join(stream.feed(chunk) for chunk in chunks)
    return out + stream.finish(), stream


def every_split(data: bytes, parts: int) -> list[list[bytes]]:
    cuts = itertools.combinations_with_replacement(range(len(data) + 1), parts - 1)
    return [[data[a:b] for a, b in zip((0, *cut), (*cut, len(data)), strict=True)] for cut in cuts]


@pytest.mark.parametrize("payload", PAYLOADS)
@pytest.mark.parametrize("before", [b"", b"motd noise\n", b"no newline noise", START[:-1], PREFIX])
def test_payload_survives_every_split_of_the_stream(payload: bytes, before: bytes) -> None:
    data = guest_stream(payload, b"end", before=before, after=b"late output")
    # Every two-way split byte by byte, and every three-way split for short streams.
    splits = every_split(data, 2) + (every_split(data, 3) if len(data) < 90 else [])
    assert splits
    for chunks in splits:
        out, stream = run_filter(chunks)
        assert out == payload, chunks
        assert stream.closing == "end"
        assert stream.done


def test_byte_by_byte_delivery_matches_one_chunk() -> None:
    for payload in PAYLOADS:
        data = guest_stream(payload, b"exit 3", before=b"noise", after=b"x")
        out, stream = run_filter([data[i : i + 1] for i in range(len(data))])
        assert (out, stream.closing) == (payload, "exit 3")


def test_payload_is_released_at_once_except_for_a_possible_closing_prefix() -> None:
    stream = StreamFilter(NONCE)
    stream.feed(START)

    assert stream.feed(b"hello") == b"hello"
    assert stream.feed(b"world\x1e") == b"world"  # the lead byte might begin a closing line
    assert stream.feed(b"x") == b"\x1ex"  # it did not
    assert stream.feed(PREFIX[:5]) == b""
    assert stream.feed(b"zzz") == PREFIX[:5] + b"zzz"


def test_nothing_before_the_start_marker_is_read_or_released() -> None:
    stream = StreamFilter(NONCE)

    assert stream.feed(b"banner " + PREFIX[:-3]) == b""
    assert not stream.started
    assert stream.feed(PREFIX[-3:] + b"start\nreal") == b"real"
    assert stream.started


def test_everything_after_the_closing_line_is_dropped() -> None:
    stream = StreamFilter(NONCE)

    assert stream.feed(guest_stream(b"out", b"end", after=b"background child output")) == b"out"
    assert stream.done
    assert stream.feed(b"more") == b""
    assert stream.finish() == b""


def test_a_parked_line_is_removed_and_reported_without_ending_the_stream() -> None:
    data = START + b"a" + PREFIX + b"parked\n" + b"b" + PREFIX + b"end\n"
    for chunks in every_split(data, 2):
        out, stream = run_filter(chunks)
        assert out == b"ab"
        assert stream.closing == "end"
    stream = StreamFilter(NONCE)
    stream.feed(START + PREFIX + b"parked\n")
    assert stream.take_parked()
    assert not stream.take_parked()
    assert not stream.done


def test_a_stream_that_ends_inside_a_closing_line_releases_nothing_of_it() -> None:
    stream = StreamFilter(NONCE)

    assert stream.feed(START + b"out" + PREFIX + b"exi") == b"out"
    assert stream.finish() == b""
    assert stream.closing is None
    assert not stream.done


def test_a_stream_that_ends_holding_a_partial_prefix_releases_it_as_payload() -> None:
    stream = StreamFilter(NONCE)

    assert stream.feed(START + b"out\x1e") == b"out"
    assert stream.finish() == b"\x1e"


@pytest.mark.parametrize("payload", PAYLOADS[:6])
def test_two_streams_give_the_same_outcome_under_every_interleaving(payload: bytes) -> None:
    out_chunks = every_split(guest_stream(payload, b"end", before=b"x"), 3)[7]
    err_chunks = every_split(guest_stream(b"E" + payload, b"exit 5", after=b"y"), 3)[11]
    slots = len(out_chunks) + len(err_chunks)
    seen = set()
    for out_positions in itertools.combinations(range(slots), len(out_chunks)):
        stdout, stderr = StreamFilter(NONCE), StreamFilter(NONCE)
        released_out = released_err = b""
        out_iter, err_iter = iter(out_chunks), iter(err_chunks)
        for slot in range(slots):
            if slot in out_positions:
                released_out += stdout.feed(next(out_iter))
            else:
                released_err += stderr.feed(next(err_iter))
        seen.add((released_out, released_err, classify(stdout, stderr, ssh_status=0)))
    assert seen == {(payload, b"E" + payload, Completed(5))}


def done(closing: bytes) -> StreamFilter:
    stream = StreamFilter(NONCE)
    stream.feed(guest_stream(b"", closing))
    return stream


def started() -> StreamFilter:
    stream = StreamFilter(NONCE)
    stream.feed(START)
    return stream


@pytest.mark.parametrize(
    ("stdout", "stderr", "ssh_status", "host_key", "expected"),
    [
        # Evidence from the markers wins over ssh's own status.
        (done(b"end"), done(b"exit 0"), 0, False, Completed(0)),
        (done(b"end"), done(b"exit 255"), 255, False, Completed(255)),
        (done(b"end"), done(b"exit 137"), None, False, Completed(137)),
        (done(b"end"), done(b"refused cwd"), 0, False, NotDelivered(NotDeliveredReason.REFUSED, "cwd")),
        (
            done(b"end"),
            done(b"refused exec ENOENT"),
            None,
            False,
            NotDelivered(NotDeliveredReason.REFUSED, "exec ENOENT"),
        ),
        # A trailer the grammar does not allow is a protocol violation, never a refusal to repeat.
        (done(b"end"), done(b"refused rm -rf /"), 0, False, Unknown(UnknownReason.TRUNCATED)),
        (done(b"end"), done(b"exit lots"), 0, False, Unknown(UnknownReason.TRUNCATED)),
        # A start marker without both closing lines.
        (started(), started(), 0, False, Unknown(UnknownReason.TRUNCATED)),
        (done(b"end"), started(), 255, False, Unknown(UnknownReason.TRUNCATED)),
        (started(), StreamFilter(NONCE), 0, False, Unknown(UnknownReason.TRUNCATED)),
        # No start marker anywhere: ssh's own status says how far it got.
        (StreamFilter(NONCE), StreamFilter(NONCE), 127, False, NotDelivered(NotDeliveredReason.PYTHON_MISSING)),
        (
            StreamFilter(NONCE),
            StreamFilter(NONCE),
            STDLIB_MISSING_STATUS,
            False,
            NotDelivered(NotDeliveredReason.STDLIB_MISSING),
        ),
        (StreamFilter(NONCE), StreamFilter(NONCE), 0, False, NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)),
        (StreamFilter(NONCE), StreamFilter(NONCE), 1, False, NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)),
        (StreamFilter(NONCE), StreamFilter(NONCE), 254, False, NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)),
        (StreamFilter(NONCE), StreamFilter(NONCE), 255, False, Unknown(UnknownReason.CONNECTION)),
        (StreamFilter(NONCE), StreamFilter(NONCE), 255, True, Unknown(UnknownReason.HOST_KEY)),
        (StreamFilter(NONCE), StreamFilter(NONCE), -15, False, Unknown(UnknownReason.CONNECTION)),
        (StreamFilter(NONCE), StreamFilter(NONCE), 0xC0000005, False, Unknown(UnknownReason.CONNECTION)),
        (StreamFilter(NONCE), StreamFilter(NONCE), None, False, Unknown(UnknownReason.CONNECTION)),
        # The host-key typing needs ssh's own 255, not just text in the log.
        (StreamFilter(NONCE), StreamFilter(NONCE), 1, True, NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)),
    ],
)
def test_classifier_row(
    stdout: StreamFilter, stderr: StreamFilter, ssh_status: int | None, host_key: bool, expected: object
) -> None:
    assert classify(stdout, stderr, ssh_status, host_key_failed=host_key) == expected


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("exec ENOENT", 127),
        ("exec EACCES", 126),
        ("exec ENOEXEC", 126),
        ("exec E2BIG", 126),
        ("exec UNKNOWN", 126),
        ("cwd", 125),
        ("handover", 125),
    ],
)
def test_refusal_statuses_follow_the_shell_convention(code: str, status: int) -> None:
    assert refusal_status(code) == status
