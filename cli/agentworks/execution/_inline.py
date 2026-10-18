"""Private identity-bound Linux inline execution and evidence candidate."""

from __future__ import annotations

import posixpath
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._borrowed_io import DiscardSink, PrefixedSource, PresentationCollector
from agentworks.execution._evidence_wire import FrameReader
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._inline_bundle import FIXED_SOURCE, PREPARED_PROGRAM, ROOT_PROGRAM
from agentworks.execution._inline_observer import InlineObservation, InlineObserver
from agentworks.execution._inline_request import (
    InlineManifest,
    InvocationKind,
    ManifestError,
    ManifestErrorCode,
    OutputMode,
    ScriptShell,
    encode_prepared_request_envelope,
    encode_request_envelope,
)
from agentworks.execution._runtime_prerequisite import (
    HelperClosureExpectation,
    RuntimePrefixSink,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    _NumericGuestBootstrap,
    build_root_guest_bootstrap_argv,
    build_runtime_identity_helper_argv,
)
from agentworks.execution.carrier import (
    CarrierIO,
    Dispatch,
    Failure,
    FiniteInput,
    LiveInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.models import Command, Script, Shell

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution._capture_control import RetrievedCapture
    from agentworks.execution._fixed_helper_operation import BoundHelperCarrier
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution.carrier import ByteSink, ByteSource, Deadline, ExitStatus


@dataclass
class PreparedInlineCandidate:
    """One non-replayable carrier attempt and its bounded evidence collector."""

    invocation: PreparedInvocation
    io: CarrierIO
    nonce: str
    output_mode: OutputMode
    closure_expectation: HelperClosureExpectation
    _runtime: RuntimePrefixSink = field(repr=False)
    _reader: FrameReader = field(repr=False)
    _observer: InlineObserver = field(repr=False)
    _claimed: bool = field(default=False, init=False, repr=False)

    def release(self) -> None:
        """Drop caller endpoints and request bytes after the sole observation."""
        if isinstance(self.io.input, LiveInput) and isinstance(self.io.input.source, PrefixedSource):
            self.io.input.source.release()
        self.io = CarrierIO()
        self._observer.release()

    def claim(self) -> None:
        if self._claimed:
            raise ValidationError("An inline candidate cannot be dispatched more than once")
        self._claimed = True


@dataclass(frozen=True, slots=True)
class InlineCandidateResult:
    """Separate carrier, runtime and helper observations, never application success."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: InlineObservation | None
    requested_output: OutputMode = OutputMode.CAPTURE
    retrieved_capture: tuple[RetrievedCapture, RetrievedCapture] | None = field(default=None, repr=False)


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
    stdin: bytes | None,
    env: Mapping[str, str] | None,
    cwd: str | None,
    output_mode: OutputMode,
    capture_limit: int,
    capture_tokens: tuple[bytes, bytes] | None = None,
) -> InlineManifest:
    if stdin is not None and type(stdin) is not bytes:
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
        capture_tokens=capture_tokens,
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
    source: ByteSource | None = None,
    stdout: ByteSink | None = None,
    stderr: ByteSink | None = None,
    runtime_selection: RuntimeSelection,
    bootstrap: _NumericGuestBootstrap | None = None,
    _prepared_input: bool = False,
    _capture_tokens: tuple[bytes, bytes] | None = None,
) -> PreparedInlineCandidate:
    """Validate all payload data and build one file-free Linux helper attempt."""
    nonce = secrets.token_hex(16)
    program = PREPARED_PROGRAM if _prepared_input else ROOT_PROGRAM
    if _capture_tokens is not None:
        from agentworks.execution._capture_bundle import CAPTURE_PROGRAM

        program = CAPTURE_PROGRAM
    if bootstrap is None:
        fixed_argv, candidates, system_shim = build_runtime_identity_helper_argv(
            plan,
            selection=runtime_selection,
            fixed_source=FIXED_SOURCE,
            nonce=nonce,
        )
    else:
        fixed_argv, candidates, system_shim = build_root_guest_bootstrap_argv(
            bootstrap.root_entry,
            plan.expected,
            selection=runtime_selection,
            program=program,
            nonce=nonce,
            expected_guest=bootstrap.guest,
        )
    if cwd is not None:
        _utf8(cwd)
        if not posixpath.isabs(cwd):
            raise ValidationError("Inline working directory must be absolute")
    if capture_limit is not None and (
        type(capture_limit) is not int
        or not 0 <= capture_limit <= (2**63 - 2 if _capture_tokens is not None else 4_096)
    ):
        raise ValidationError("Inline capture limit must be between 0 and 4096 bytes")
    if stdout is not None and stderr is not None:
        output_mode, retained_limit = OutputMode.DELIVER, 0
    elif stdout is not None or stderr is not None:
        raise ValidationError("Inline delivery requires both application sinks")
    elif sensitive:
        output_mode, retained_limit = OutputMode.SUPPRESS, 0
    elif capture_limit is None:
        output_mode, retained_limit = OutputMode.DISCARD, 0
    else:
        output_mode, retained_limit = (
            (OutputMode.SCRATCH_CAPTURE if _capture_tokens is not None else OutputMode.CAPTURE),
            capture_limit,
        )
    manifest = _manifest(
        request,
        nonce=nonce,
        identity=plan.expected,
        stdin=None if source is not None else stdin,
        env=env,
        cwd=cwd,
        output_mode=output_mode,
        capture_limit=retained_limit,
        capture_tokens=_capture_tokens,
    )
    manifest_error: ManifestErrorCode | None = None
    manifest_data = b""
    try:
        if _capture_tokens is not None:
            from agentworks.execution._inline_request import encode_capture_request_envelope

            manifest_data = program.prefix + encode_capture_request_envelope(manifest)
        else:
            manifest_data = (
                PREPARED_PROGRAM.prefix + encode_prepared_request_envelope(manifest)
                if _prepared_input
                else encode_request_envelope(manifest)
            )
    except ManifestError as error:
        manifest_error = error.code
    if manifest_error is ManifestErrorCode.OVERSIZED:
        raise ValidationError("Execution invocation exceeds its fixed manifest bound")
    if manifest_error is not None:
        raise ValidationError("Inline invocation contains an invalid field")

    observer = InlineObserver(
        output_mode,
        retained_limit,
        stdout=stdout,
        stderr=stderr,
        capture_tokens=_capture_tokens,
        capture_identity=plan.expected,
    )
    reader = FrameReader(nonce, observer.accept)
    collector = PresentationCollector(reader, observer)
    runtime = RuntimePrefixSink(nonce, candidates, collector, system_shim)
    return PreparedInlineCandidate(
        invocation=PreparedInvocation(fixed_argv),
        io=CarrierIO(
            input=FiniteInput(manifest_data, sensitive=sensitive)
            if source is None
            else LiveInput(PrefixedSource(manifest_data, source), sensitive=sensitive),
            output=SinkOutput(runtime, DiscardSink(), require_live=output_mode is OutputMode.DELIVER),
            sensitive=sensitive,
        ),
        nonce=nonce,
        output_mode=output_mode,
        closure_expectation=HelperClosureExpectation(
            nonce, candidates, system_shim, bootstrap.guest if bootstrap is not None else None
        ),
        _runtime=runtime,
        _reader=reader,
        _observer=observer,
    )


def execute_inline_candidate(
    carrier: BoundHelperCarrier,
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
        runtime_prerequisite = prepared._runtime.observation
        prepared._runtime.clear()
        prepared.release()
    if runtime_prerequisite.state is RuntimePrerequisiteState.READY:
        delivered = report.stdout.retention is Retention.DELIVERED and report.stderr.retention is Retention.DELIVERED
        observation = prepared._observer.finish(
            prepared._reader.error,
            carrier_stdout_complete=delivered and report.stdout.complete,
        )
    else:
        observation = None
    return InlineCandidateResult(
        dispatch=report.dispatch,
        carrier_completion=report.completion,
        carrier_local_status=report.local_status,
        carrier_failure=report.failure,
        runtime_prerequisite=runtime_prerequisite,
        observation=observation,
        requested_output=prepared.output_mode,
    )
