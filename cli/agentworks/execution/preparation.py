"""Bounded Linux command/script preparation for the shared carrier proof.

This slice deliberately refuses startup modes and interpreters not yet proven.
It does not select identity, grant permission, stage files, or interpret carrier
completion evidence. The input envelope's 256 KiB bound is a proof limit, not
the eventual execution API's script/file size policy.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution.bootstrap import BOOTSTRAP_ARGV
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    FiniteInput,
    PreparedInvocation,
    Provenance,
    Retention,
)
from agentworks.execution.models import Command, Script

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_ENVELOPE_BYTES = 262_144
_RESERVED_ENV = frozenset({"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD"})


@dataclass(frozen=True)
class PreparedExecution:
    """One carrier invocation whose finite envelope lives only in CarrierIO."""

    invocation: PreparedInvocation
    io: CarrierIO
    token: str


@dataclass(frozen=True)
class DecodedOutput:
    """Guest bytes and framing facts, never application completion evidence."""

    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    stdout_complete: bool = False
    stderr_complete: bool = False
    suppressed: bool = False
    bootstrap_failed: bool = False
    framing_error: bool = False


def _text(value: str) -> bytes:
    if not isinstance(value, str) or "\0" in value:
        raise ValidationError("Textual execution fields must be non-NUL strings")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        pass
    # A chained codec error retains the complete payload, even with from None.
    raise ValidationError("Textual execution fields must be valid UTF-8")


def prepare(
    request: Command | Script,
    *,
    stdin: bytes = b"",
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    sensitive: bool = False,
) -> PreparedExecution:
    """Validate caller payload and build one bounded ASCII input envelope.

    The caller supplies already-composed environment sensitivity. The proof does
    not resolve secrets or permit environment keys that undo bootstrap startup
    isolation. Destination-default shell lookup happens inside the execution
    identity and refuses accounts whose configured shell is not sh or bash.
    """
    if not isinstance(stdin, bytes):
        raise ValidationError("Finite application input must be bytes")
    if isinstance(request, Command):
        kind, shell, arguments, source = "command", "none", request.argv, b""
    elif isinstance(request, Script):
        if request.login or request.interactive:
            raise ValidationError("Login and interactive startup are not proven by the buffered Linux slice")
        kind, shell, arguments, source = "script", request.shell.value, (), _text(request.source)
    else:
        raise ValidationError("Preparation requires a literal command or explicit script")
    environment = dict(env or {})
    if len(arguments) > 1024 or len(environment) > 1024:
        raise ValidationError("The bounded proof accepts at most 1024 arguments and environment entries")
    token = secrets.token_hex(16)
    lines = [b"AGW1", token.encode("ascii"), b"1" if sensitive else b"0", kind.encode(), shell.encode()]
    lines.append(str(len(arguments)).encode())
    lines.extend(base64.b64encode(_text(argument)) for argument in arguments)
    lines.append(str(len(environment)).encode())
    for key, value in environment.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise ValidationError("Execution environment requires valid variable names")
        if key.startswith("_agw_") or key in _RESERVED_ENV:
            raise ValidationError("Environment key conflicts with the bounded proof's startup isolation")
        lines.extend((key.encode("ascii"), base64.b64encode(_text(value))))
    if cwd == "":
        raise ValidationError("An explicit working directory cannot be empty")
    lines.extend((base64.b64encode(_text(cwd) if cwd is not None else b""), base64.b64encode(source)))
    lines.append(base64.b64encode(stdin))
    envelope = b"\n".join(lines) + b"\n"
    if len(envelope) > MAX_ENVELOPE_BYTES:
        raise ValidationError("Invocation exceeds the bounded proof's input envelope limit")
    return PreparedExecution(
        PreparedInvocation(BOOTSTRAP_ARGV),
        CarrierIO(input=FiniteInput(envelope, sensitive=sensitive), sensitive=sensitive),
        token,
    )


def decode_output(prepared: PreparedExecution, output: CapturedOutput) -> DecodedOutput:
    """Separate armored guest streams without trusting raw carrier stderr.

    Unknown/malformed records are not guest output or safe diagnostics. Preserve
    already decoded bytes but mark both streams incomplete. End markers establish
    only stream closure, not execution completion; a carrier may still report an
    ambiguous outcome. Suppressed output deliberately supplies no framing proof.
    """
    if prepared.io.sensitive or output.retention == Retention.SUPPRESSED:
        return DecodedOutput(suppressed=True)
    if output.retention == Retention.DISCARDED:
        return DecodedOutput()
    if output.provenance != Provenance.CARRIER_STDOUT:
        return DecodedOutput(framing_error=True)
    prefix = prepared.token.encode("ascii") + b" "
    streams: dict[bytes, bytearray] = {b"O": bytearray(), b"E": bytearray()}
    ended: set[bytes] = set()
    started = failed = invalid = False
    records = output.data.split(b"\n")
    if records[-1]:
        invalid = True
    for record in records[:-1]:
        if record == prefix + b"F":
            failed = True
            continue
        if record == prefix + b"B" and not started and not failed:
            started = True
            continue
        if not started or failed or not record.startswith(prefix):
            invalid = True
            break
        fields = record[len(prefix) :].split(b" ")
        if len(fields) != 2 or fields[0] not in streams or fields[0] in ended:
            invalid = True
            break
        tag, payload = fields
        if payload == b"!":
            ended.add(tag)
            continue
        try:
            if not payload or len(payload) > 76:
                raise ValueError
            streams[tag].extend(base64.b64decode(payload, validate=True))
        except (ValueError, binascii.Error):
            invalid = True
            break
    complete = output.complete and started and not failed and not invalid
    return DecodedOutput(
        stdout=bytes(streams[b"O"]),
        stderr=bytes(streams[b"E"]),
        stdout_complete=complete and b"O" in ended,
        stderr_complete=complete and b"E" in ended,
        bootstrap_failed=failed,
        framing_error=invalid or not started,
    )
