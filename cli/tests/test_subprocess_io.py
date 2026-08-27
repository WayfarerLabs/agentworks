r"""Byte-exact stdin delivery to child processes.

The bug these guard against is platform-specific: ``text=True`` rewrites
``\n`` to ``os.linesep`` on the way into the child, which on Windows appends a
carriage return to every line. CI runs Linux, where that rewriting does not
happen, so the round-trip test below cannot fail there. The assertions that
stdin is handed over as ``bytes`` are what hold on every platform, and the
per-transport tests make the same assertion at their own call sites.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from agentworks.subprocess_io import decode_stream, stdin_bytes

_ECHO_STDIN = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


def test_stdin_bytes_encodes_as_utf8() -> None:
    assert stdin_bytes("tskey-abc") == b"tskey-abc"


def test_stdin_bytes_passes_absent_payload_through() -> None:
    assert stdin_bytes(None) is None


def test_decode_stream_replaces_undecodable_bytes() -> None:
    assert decode_stream(b"ok\xff") == "ok\ufffd"


def test_stdin_reaches_the_child_byte_exact() -> None:
    """A trailing newline stays one byte, so a line-oriented reader sees one line."""
    payload = "tskey-auth-sentinel\n"

    result = subprocess.run(
        [sys.executable, "-c", _ECHO_STDIN],
        input=stdin_bytes(payload),
        capture_output=True,
        timeout=60,
    )

    assert result.stdout == b"tskey-auth-sentinel\n"
    assert decode_stream(result.stdout) == payload


def test_embedded_newlines_are_not_rewritten() -> None:
    """The Lima provider YAML crosses this same pipe, multi-line."""
    payload = "images:\n- location: file\ncpus: 4\n"

    result = subprocess.run(
        [sys.executable, "-c", _ECHO_STDIN],
        input=stdin_bytes(payload),
        capture_output=True,
        timeout=60,
    )

    assert decode_stream(result.stdout) == payload
    assert b"\r" not in result.stdout


def test_stdin_bytes_round_trips_os_sourced_bytes() -> None:
    """A value decoded by surrogateescape goes back out as its original byte."""
    assert stdin_bytes("tskey-" + chr(0xDCFF)) == b"tskey-\xff"


def test_decode_stream_folds_newlines_like_text_mode() -> None:
    """Text mode folded CRLF and lone CR through universal newlines.

    Byte mode only changes what crosses stdin, so what comes back is decoded
    exactly as before. Consumers that compare output without stripping it
    depend on this.
    """
    assert decode_stream(b"X\r\nY\rZ\n") == "X\nY\nZ\n"


def test_stdin_bytes_keeps_an_unencodable_payload_out_of_the_raise() -> None:
    """The payload here is a secret, and UnicodeEncodeError carries it verbatim."""
    secret = "tskey-auth-swordfish-" + chr(0xD800)

    with pytest.raises(ValueError) as caught:
        stdin_bytes(secret)

    assert "swordfish" not in str(caught.value)
    assert "swordfish" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
