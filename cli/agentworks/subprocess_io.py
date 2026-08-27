r"""Byte-exact stdin for child processes, and decoding that text mode's equal.

``subprocess.run(..., text=True)`` wraps the child's stdin in a
``TextIOWrapper`` under default newline handling, which rewrites every LF to
``os.linesep``. On Windows that is CRLF, so a line-oriented payload reaches the
child corrupted: a value written as one line plus LF arrives at a remote
``read`` carrying a trailing CR, which then travels on into whatever consumes
the value.

Byte mode is the only cure. ``subprocess.Popen`` takes no ``newline``
parameter, so text mode cannot be told to stop translating.

Callers still want decoded output, so the pair here splits the two concerns.
Hand stdin over with :func:`stdin_bytes`, drop ``text=True`` and its
``encoding`` and ``errors`` companions, then decode the captured streams with
:func:`decode_stream`. Keeping the ``subprocess.run`` call itself in the
calling module is deliberate: the child process, its argv, its timeout policy,
and its failure handling all belong to that module, and one wrapper taking the
union of four different policies would be a bag of flags.

Only a call that writes stdin needs byte mode. A call that merely captures
output has nothing to corrupt and stays in text mode.

:func:`decode_stream` folds CRLF and lone CR to LF because text mode did that
too, through universal newlines, in the direction that was never broken.
Folding here keeps this module about stdin alone and keeps every
``Transport.run`` returning output under one rule; ``vms/initializer/packages``
compares command output against an expected string without stripping it, and
is one of the consumers that rule protects.

Line-oriented secret delivery is what makes the stdin half matter.
``require_line_safe_secret`` rejects a value carrying CR, LF, or NUL precisely
so the consumer receives one clean line, and a transport that appends its own
CR breaks that guarantee after the check has passed.

The output side of this same platform trap is handled once at the process
boundary, in ``agentworks/cli/_entry.py``, which forces bare-LF on stdout so a
stray CR cannot ride into a consumer's next argument.
"""

from __future__ import annotations


def stdin_bytes(input_text: str | None) -> bytes | None:
    """Encode ``input_text`` for a byte-mode ``subprocess`` stdin pipe.

    ``errors="surrogateescape"`` because byte-exact delivery is the whole
    point: a value that reached us as raw OS bytes, which is how ``os.environ``
    decodes, goes back out as those same bytes. Text mode applied
    ``errors="replace"`` here, silently substituting a replacement character
    into the payload, and a silently corrupted secret is the exact failure this
    module exists to prevent.

    ``None`` passes through so a caller can forward an absent payload without
    branching.

    A payload that is malformed beyond surrogateescape raises, and the raise is
    rebuilt: ``UnicodeEncodeError`` carries the whole offending string on
    ``.object`` and in its ``repr``, which for these callers is a resolved
    secret. Callers get a value-free error with no chained cause instead.
    """
    if input_text is None:
        return None
    try:
        return input_text.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError:
        pass
    # Raised clear of the except block on purpose: ``from None`` would still
    # leave the original exception, payload and all, on ``__context__``.
    raise ValueError("stdin payload is not encodable as UTF-8")


def decode_stream(raw: bytes) -> str:
    """Decode one captured stream exactly as text mode decoded it.

    ``errors="replace"`` and the newline folding together reproduce
    ``text=True, encoding="utf-8", errors="replace"``, so moving a call site to
    byte mode changes what it writes to stdin and nothing about what it reads
    back.
    """
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
