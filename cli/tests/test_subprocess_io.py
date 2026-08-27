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

from agentworks.subprocess_io import decode_stream, stdin_bytes

_ECHO_STDIN = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


def test_stdin_bytes_encodes_as_utf8() -> None:
    assert stdin_bytes("tskey-abc") == b"tskey-abc"


def test_stdin_bytes_passes_absent_payload_through() -> None:
    assert stdin_bytes(None) is None


def test_stdin_bytes_never_returns_text() -> None:
    """Text would put the pipe back in translating mode on Windows."""
    assert isinstance(stdin_bytes("value"), bytes)


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
