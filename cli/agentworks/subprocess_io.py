r"""Subprocess stdin and output helpers that do not rewrite bytes.

``subprocess.run(..., text=True)`` wraps the child's stdin in a
``TextIOWrapper`` under default newline handling, which rewrites every ``\n``
to ``os.linesep``. On Windows that is ``\r\n``, so a line-oriented payload
reaches the child corrupted: a value written as ``secret\n`` arrives at a
remote ``read`` as ``secret\r``, and the trailing carriage return travels on
into whatever consumes the value.

Callers still want decoded stdout and stderr, so the pair here splits the two
concerns. Hand stdin over as bytes with :func:`stdin_bytes`, drop ``text=True``
and its ``encoding`` / ``errors`` companions, then decode the captured streams
with :func:`decode_stream`. Keeping the ``subprocess.run`` call itself in the
calling module is deliberate: the child process, its argv, and its failure
handling all belong to that module.

Line-oriented secret delivery depends on this. ``require_line_safe_secret``
rejects a value carrying CR, LF, or NUL precisely so the consumer receives one
clean line; a transport that appends its own CR breaks that guarantee after the
check has passed.
"""

from __future__ import annotations


def stdin_bytes(input_text: str | None) -> bytes | None:
    """Encode ``input_text`` for a byte-mode ``subprocess`` stdin pipe.

    ``None`` passes through so a caller can forward an absent payload without
    branching.
    """
    return None if input_text is None else input_text.encode("utf-8")


def decode_stream(raw: bytes) -> str:
    """Decode one captured stream, replacing undecodable bytes.

    Matches what ``encoding="utf-8", errors="replace"`` gave these call sites
    before stdin moved to byte mode.
    """
    return raw.decode("utf-8", errors="replace")
