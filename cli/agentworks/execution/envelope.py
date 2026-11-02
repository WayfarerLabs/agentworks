"""The wire between the client and agw-launch: frames, the envelope and the markers.

The client writes three things to ssh's stdin, in order: the launcher source
frame, the envelope frame, then the caller's input untouched. A frame is ten
ASCII decimal digits, zero padded, then that many bytes.

The envelope is one JSON object, ASCII-only whatever the workstation's locale.
``encode`` is the one function that turns a request into bytes, so it is where
operator-authored input is validated. Everything the guest prints for the
client is a marker line: ``0x1E``, the request's nonce, a space, a word and a
newline. A :class:`StreamFilter` per stream finds the start marker, passes
payload bytes through and stops at the closing line; :func:`classify` combines
both filters with ssh's own exit status.
"""

from __future__ import annotations

import enum
import functools
import json
import re
import secrets
from importlib import resources
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution.agw_launch import STDLIB_MISSING_STATUS
from agentworks.execution.requests import Command, LoginShell, Script
from agentworks.execution.results import (
    REFUSAL_CODE,
    Completed,
    Completion,
    NotDelivered,
    NotDeliveredReason,
    Unknown,
    UnknownReason,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution.requests import Request

# Linux caps each argv element and each NAME=VALUE environment string at
# MAX_ARG_STRLEN, 131,072 bytes counting the NUL (measured: 131,071 runs,
# 131,072 fails). The client refuses a larger one; a total over ARG_MAX stays
# the guest's `refused exec E2BIG`.
MAX_STRING_BYTES = 131_071

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXIT_LINE = re.compile(r"exit (\d{1,3})")
_MAX_CLOSING_LINE = 4096
DEFAULT_TERM = "xterm-256color"


def new_nonce() -> str:
    """A fresh per-request nonce: 32 lowercase hex characters, never in the payload's argv or environment."""
    return secrets.token_hex(16)


def new_handover_token() -> str:
    """The client's name for a terminal handover's socket directory: 16 lowercase hex characters."""
    return secrets.token_hex(8)


def frame(payload: bytes) -> bytes:
    """``payload`` behind its ten-digit length."""
    return b"%010d" % len(payload) + payload


@functools.cache
def launcher_source() -> bytes:
    """The text of ``agw_launch.py``, which stage 0 runs on the guest."""
    return resources.files("agentworks.execution").joinpath("agw_launch.py").read_bytes()


def _text(value: str, what: str) -> bytes:
    # Boundary: environment, arguments, working directory and script text come
    # from operator-authored config and from secret backends. The message names
    # what was refused and never quotes it; the chained codec error would.
    if "\0" in value:
        raise ValidationError(f"{what} contains a NUL byte")
    try:
        return value.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raise ValidationError(f"{what} cannot be encoded as bytes") from None


def encode(request: Request, *, nonce: str, term: str | None = None, handover: str | None = None) -> bytes:
    """The envelope for ``request``, unframed.

    A :class:`LoginShell` is the one request that travels as a handover, so it
    needs the ``handover`` token and the operator's ``term``; every other
    request runs in ``run`` mode and takes neither.
    """
    if isinstance(request, LoginShell):
        if handover is None or term is None:
            raise ValueError("a login shell travels as a handover and needs its token and TERM")
        mode, kind = "park", "shell"
    else:
        mode, kind = "run", "argv" if isinstance(request, Command) else "script"
        if handover is not None or term is not None:
            raise ValueError("only a login shell takes a handover or TERM")
    body: dict[str, object] = {
        "nonce": nonce,
        "mode": mode,
        "kind": kind,
        "cwd": _checked_cwd(request.cwd),
        "env": _checked_env(request.env),
        "term": None if term is None else _checked_term(term),
        "handover": handover,
    }
    if isinstance(request, Command):
        body["argv"] = _checked_argv(request.argv)
    elif isinstance(request, Script):
        _text(request.source, "script source")
        body.update(shell=request.shell.value, source=request.source, login=request.login)
    return json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def message(request: Request, *, nonce: str, term: str | None = None, handover: str | None = None) -> bytes:
    """What a request writes to ssh's stdin ahead of the caller's input: the launcher frame, then the envelope frame."""
    return frame(launcher_source()) + frame(encode(request, nonce=nonce, term=term, handover=handover))


def _checked_cwd(cwd: str | None) -> str | None:
    if cwd is None:
        return None
    _text(cwd, "working directory")
    if not cwd.startswith("/"):
        raise ValidationError("the working directory must be an absolute path")
    return cwd


def _checked_term(term: str) -> str:
    _text(term, "TERM")
    return term


def _checked_argv(argv: tuple[str, ...]) -> list[str]:
    if not argv or not argv[0]:
        raise ValidationError("a command needs a non-empty program name")
    for position, argument in enumerate(argv):
        if len(_text(argument, f"argument {position}")) > MAX_STRING_BYTES:
            raise ValidationError(f"argument {position} is longer than {MAX_STRING_BYTES} bytes")
    return list(argv)


def _checked_env(env: Mapping[str, str]) -> dict[str, str]:
    checked: dict[str, str] = {}
    for name, value in env.items():
        if not _ENV_NAME.fullmatch(name):
            raise ValidationError("an environment variable name must match [A-Za-z_][A-Za-z0-9_]*")
        encoded = _text(value, f"environment variable {name}")
        if len(name) + 1 + len(encoded) > MAX_STRING_BYTES:
            raise ValidationError(f"environment variable {name} is longer than {MAX_STRING_BYTES} bytes")
        checked[name] = value
    return checked


# -- Markers and the stream filters ------------------------------------------------------


def marker_prefix(nonce: str) -> bytes:
    """What every marker line starts with: the lead byte, the nonce and a space.

    The lead byte is one no text emits, so the filters' hold-back almost never
    delays a real byte; a lead hex digit would hold back one in sixteen output
    endings.
    """
    return b"\x1e" + nonce.encode("ascii") + b" "


class _Phase(enum.Enum):
    SEEKING = enum.auto()
    PASSING = enum.auto()
    DONE = enum.auto()


class StreamFilter:
    """Extracts one stream's payload from the guest's output.

    - Seeking: search for the start marker, dropping everything before it and
      keeping only a tail of marker length across chunks. Nothing in the
      dropped bytes is read.
    - Passing: return payload bytes at once, holding back only the longest
      suffix that is a proper prefix of the closing prefix. No timer is
      involved. A ``parked`` line is removed from the stream and recorded.
    - Done: after the closing line, drop everything.
    """

    def __init__(self, nonce: str) -> None:
        self._prefix = marker_prefix(nonce)
        self._start = self._prefix + b"start\n"
        self._phase = _Phase.SEEKING
        self._held = b""
        self._line: bytearray | None = None
        self._parked = False
        self.closing: str | None = None
        """The word of the closing line (``end``, ``exit N`` or ``refused CODE``), once seen."""

    @property
    def started(self) -> bool:
        return self._phase is not _Phase.SEEKING

    @property
    def done(self) -> bool:
        return self._phase is _Phase.DONE

    @property
    def parked(self) -> bool:
        """Whether a ``parked`` line has arrived. Sticky: a parking connection sends it once."""
        return self._parked

    def feed(self, data: bytes) -> bytes:
        """Take the next chunk of the stream; return the payload bytes it releases."""
        out = bytearray()
        buffer = self._held + data
        self._held = b""
        while buffer and self._phase is not _Phase.DONE:
            if self._phase is _Phase.SEEKING:
                found = buffer.find(self._start)
                if found < 0:
                    self._held = buffer[-(len(self._start) - 1) :]
                    break
                buffer = buffer[found + len(self._start) :]
                self._phase = _Phase.PASSING
            elif self._line is not None:
                buffer = self._read_closing_line(buffer)
            else:
                found = buffer.find(self._prefix)
                if found >= 0:
                    out += buffer[:found]
                    buffer = buffer[found + len(self._prefix) :]
                    self._line = bytearray()
                else:
                    held = self._partial_prefix_length(buffer)
                    out += buffer[: len(buffer) - held]
                    self._held = buffer[len(buffer) - held :]
                    break
        return bytes(out)

    def finish(self) -> bytes:
        """The stream ended: release bytes held back because they might have begun a closing line."""
        held, self._held = self._held, b""
        return held if self._phase is _Phase.PASSING and self._line is None else b""

    def _read_closing_line(self, buffer: bytes) -> bytes:
        assert self._line is not None
        newline = buffer.find(b"\n")
        if newline < 0:
            self._line += buffer
            if len(self._line) > _MAX_CLOSING_LINE:
                self._close("")
            return b""
        self._line += buffer[:newline]
        word = bytes(self._line).decode("ascii", "replace")
        self._line = None
        if word == "parked":
            self._parked = True
        else:
            self._close(word)
        return buffer[newline + 1 :]

    def _close(self, word: str) -> None:
        self.closing = word
        self._phase = _Phase.DONE
        self._line = None

    def _partial_prefix_length(self, buffer: bytes) -> int:
        for length in range(min(len(self._prefix) - 1, len(buffer)), 0, -1):
            if buffer.endswith(self._prefix[:length]):
                return length
        return 0


def classify(
    stdout: StreamFilter,
    stderr: StreamFilter,
    ssh_status: int | None,
    *,
    host_key_failed: bool = False,
    auth_failed: bool = False,
    stdout_abandoned: bool = False,
    stderr_abandoned: bool = False,
) -> Completion:
    """Combine both filters and ssh's exit status into a completion.

    ``ssh_status`` is ssh's own status when it exited by itself, and ``None``
    when it was still running or was killed (a signal's negative status is not
    evidence either). A deadline or an output limit is the caller's to report.
    ``host_key_failed`` and ``auth_failed`` say what ssh's own log holds; they
    type only ssh's 255 with no start marker, the host key first.

    An abandoned stream is one whose consumer left, so the client closed it
    unread. An abandoned stdout stands in for the ``end`` line; the stderr
    trailer still decides. A departed consumer without a trailer is
    ``Unknown(CONSUMER_LEFT)``.
    """
    if (stdout.closing == "end" or stdout_abandoned) and stderr.closing is not None:
        trailer = stderr.closing
        if (match := _EXIT_LINE.fullmatch(trailer)) is not None:
            return Completed(int(match[1]))
        code = trailer.removeprefix("refused ")
        if trailer.startswith("refused ") and REFUSAL_CODE.fullmatch(code):
            return NotDelivered(NotDeliveredReason.REFUSED, code)
        return Unknown(UnknownReason.TRUNCATED)
    if stdout_abandoned or stderr_abandoned:
        return Unknown(UnknownReason.CONSUMER_LEFT)
    if stdout.started or stderr.started:
        return Unknown(UnknownReason.TRUNCATED)
    if ssh_status == 127:
        return NotDelivered(NotDeliveredReason.PYTHON_MISSING)
    if ssh_status == STDLIB_MISSING_STATUS:
        return NotDelivered(NotDeliveredReason.STDLIB_MISSING)
    if ssh_status is not None and 0 <= ssh_status <= 254:
        return NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)
    if ssh_status == 255 and host_key_failed:
        return Unknown(UnknownReason.HOST_KEY)
    if ssh_status == 255 and auth_failed:
        return Unknown(UnknownReason.AUTH_FAILED)
    return Unknown(UnknownReason.CONNECTION)
