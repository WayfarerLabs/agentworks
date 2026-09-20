"""Private identity-bound Linux inline execution and evidence candidate."""

from __future__ import annotations

import posixpath
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._evidence_wire import FrameReader
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityPlan, build_helper_argv
from agentworks.execution._inline_bundle import FIXED_SOURCE
from agentworks.execution._inline_observer import InlineObservation, InlineObserver
from agentworks.execution._inline_request import (
    InlineManifest,
    InvocationKind,
    ManifestError,
    ManifestErrorCode,
    OutputMode,
    ScriptShell,
    encode_manifest,
)
from agentworks.execution.carrier import (
    CarrierIO,
    Dispatch,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.models import Command, Script, Shell

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution.carrier import Carrier, Deadline, ExitStatus

_DEFAULT_RUNTIME = "/usr/bin/python3"


class _DiscardSink:
    """Consume borrowed diagnostic bytes without retaining their contents."""

    @staticmethod
    def try_write(data: memoryview) -> int:
        return len(data)


@dataclass
class PreparedInlineCandidate:
    """One non-replayable carrier attempt and its bounded evidence collector."""

    invocation: PreparedInvocation
    io: CarrierIO
    nonce: str
    _reader: FrameReader = field(repr=False)
    _observer: InlineObserver = field(repr=False)
    _claimed: bool = field(default=False, init=False, repr=False)

    def claim(self) -> None:
        if self._claimed:
            raise ValidationError("An inline candidate cannot be dispatched more than once")
        self._claimed = True


@dataclass(frozen=True, slots=True)
class InlineCandidateResult:
    """Separate carrier and helper observations, never application success."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    observation: InlineObservation


def _utf8(value: str) -> bytes:
    if type(value) is not str or "\0" in value:
        raise ValidationError("Inline textual fields must be non-NUL strings")
    failed = False
    encoded = b""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise ValidationError("Inline textual fields must be valid UTF-8")
    return encoded


def _environment(env: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if env is None:
        return ()
    failed = False
    items: tuple[object, ...] = ()
    try:
        items = tuple(env.items())
    except Exception:
        failed = True
    if failed:
        raise ValidationError("Inline environment must be a string mapping")
    environment: list[tuple[str, str]] = []
    for entry in items:
        if type(entry) is not tuple or len(entry) != 2:
            raise ValidationError("Inline environment must be a string mapping")
        name, value = entry
        if type(name) is not str or type(value) is not str:
            raise ValidationError("Inline environment must be a string mapping")
        _utf8(name)
        _utf8(value)
        environment.append((name, value))
    return tuple(sorted(environment))


def _manifest(
    request: Command | Script,
    *,
    nonce: str,
    identity: IdentityExpectation,
    stdin: bytes,
    env: Mapping[str, str] | None,
    cwd: str | None,
    output_mode: OutputMode,
    capture_limit: int,
) -> InlineManifest:
    if type(stdin) is not bytes:
        raise ValidationError("Inline application input must be bytes")
    if type(identity) is not IdentityExpectation:
        raise ValidationError("Inline execution requires a bound identity expectation")
    if isinstance(request, Command):
        kind = InvocationKind.COMMAND
        argv = request.argv
        for argument in argv:
            _utf8(argument)
        source = None
        shell = None
    elif isinstance(request, Script):
        if request.login or request.interactive:
            raise ValidationError("Inline execution does not support login or interactive startup")
        kind = InvocationKind.SCRIPT
        argv = ()
        source = _utf8(request.source)
        shell = {
            Shell.SH: ScriptShell.SH,
            Shell.BASH: ScriptShell.BASH,
            Shell.USER_DEFAULT: ScriptShell.USER_DEFAULT,
        }[request.shell]
    else:
        raise ValidationError("Inline execution requires a command or script")
    return InlineManifest(
        nonce=nonce,
        kind=kind,
        argv=argv,
        source=source,
        shell=shell,
        env=_environment(env),
        cwd=cwd,
        stdin=stdin,
        output_mode=output_mode,
        capture_limit=capture_limit,
        identity=identity,
    )


def prepare_inline_candidate(
    request: Command | Script,
    *,
    plan: IdentityPlan,
    stdin: bytes = b"",
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    capture_limit: int | None = 4_096,
    sensitive: bool = False,
    runtime_path: str = _DEFAULT_RUNTIME,
) -> PreparedInlineCandidate:
    """Validate all payload data and build one file-free Linux helper attempt."""
    nonce = secrets.token_hex(16)
    fixed_argv = build_helper_argv(plan, runtime_path=runtime_path, fixed_source=FIXED_SOURCE, nonce=nonce)
    if cwd is not None:
        _utf8(cwd)
        if not posixpath.isabs(cwd):
            raise ValidationError("Inline working directory must be absolute")
    if capture_limit is not None and (type(capture_limit) is not int or not 0 <= capture_limit <= 4_096):
        raise ValidationError("Inline capture limit must be between 0 and 4096 bytes")
    if sensitive:
        output_mode, retained_limit = OutputMode.SUPPRESS, 0
    elif capture_limit is None:
        output_mode, retained_limit = OutputMode.DISCARD, 0
    else:
        output_mode, retained_limit = OutputMode.CAPTURE, capture_limit
    manifest = _manifest(
        request,
        nonce=nonce,
        identity=plan.expected,
        stdin=stdin,
        env=env,
        cwd=cwd,
        output_mode=output_mode,
        capture_limit=retained_limit,
    )
    manifest_error: ManifestErrorCode | None = None
    manifest_data = b""
    try:
        manifest_data = encode_manifest(manifest)
    except ManifestError as error:
        manifest_error = error.code
    if manifest_error is ManifestErrorCode.OVERSIZED:
        raise ValidationError("Inline invocation exceeds the 32768-byte candidate manifest bound")
    if manifest_error is not None:
        raise ValidationError("Inline invocation contains an invalid field")

    observer = InlineObserver(output_mode, retained_limit)
    reader = FrameReader(nonce, observer.accept)
    return PreparedInlineCandidate(
        invocation=PreparedInvocation(fixed_argv),
        io=CarrierIO(
            input=FiniteInput(manifest_data, sensitive=sensitive),
            output=SinkOutput(reader, _DiscardSink(), require_live=False),
            sensitive=sensitive,
        ),
        nonce=nonce,
        _reader=reader,
        _observer=observer,
    )


def execute_inline_candidate(
    carrier: Carrier,
    prepared: PreparedInlineCandidate,
    *,
    deadline: Deadline,
) -> InlineCandidateResult:
    """Make exactly one carrier call and interpret only helper-framed evidence."""
    prepared.claim()
    try:
        report = carrier.execute(prepared.invocation, io=prepared.io, deadline=deadline)
    finally:
        prepared._reader.finish()
    delivered = report.stdout.retention is Retention.DELIVERED and report.stderr.retention is Retention.DELIVERED
    observation = prepared._observer.finish(
        prepared._reader.error,
        carrier_stdout_complete=delivered and report.stdout.complete,
    )
    return InlineCandidateResult(
        dispatch=report.dispatch,
        carrier_completion=report.completion,
        carrier_local_status=report.local_status,
        carrier_failure=report.failure,
        observation=observation,
    )
