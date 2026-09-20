"""Destination entry point for the fixed private Linux inline helper."""

from __future__ import annotations

import hashlib
import os
import sys
from contextlib import suppress
from dataclasses import dataclass

from ._evidence_wire import Frame, FrameKind, encode_frame
from ._helper_identity import matches_current_identity
from ._inline_control import (
    FailureCode,
    FailureFact,
    FailurePhase,
    StreamEnd,
    StreamName,
    StreamRetention,
    WaitFact,
    WaitKind,
    empty_body,
    encode_failure,
    encode_stream_end,
    encode_wait,
)
from ._inline_request import (
    MAX_MANIFEST_BYTES,
    InlineManifest,
    InvocationKind,
    ManifestError,
    ManifestErrorCode,
    OutputMode,
    ScriptShell,
    decode_manifest,
)
from ._process import (
    Deadline,
    ProcessFailure,
    ProcessInput,
    ProcessOutput,
    ProcessResult,
    StreamResult,
    run_owned_process,
)

_SHELL_ALIASES = {
    "/bin/bash": ScriptShell.BASH,
    "/bin/sh": ScriptShell.SH,
    "/usr/bin/bash": ScriptShell.BASH,
    "/usr/bin/sh": ScriptShell.SH,
}
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class _SafeFailure(Exception):
    def __init__(self, fact: FailureFact) -> None:
        self.fact = fact
        super().__init__(fact.code.value)


class _Emitter:
    def __init__(self, nonce: str) -> None:
        self._nonce = nonce
        self._sequence = 0
        # A record always begins on a fresh line, even after unterminated account-hook output.
        self._write(b"\n")

    def emit(self, kind: FrameKind, body: bytes) -> None:
        record = encode_frame(self._nonce, Frame(self._sequence, kind, body))
        self._write(record)
        self._sequence += 1

    @staticmethod
    def _write(data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = os.write(1, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]


@dataclass(frozen=True)
class _Launch:
    argv: list[str]
    source_fd: int | None


def _script_shell(manifest: InlineManifest) -> tuple[str, ScriptShell]:
    assert manifest.shell is not None
    if manifest.shell is ScriptShell.SH:
        return "/bin/sh", ScriptShell.SH
    if manifest.shell is ScriptShell.BASH:
        return "/bin/bash", ScriptShell.BASH
    import pwd

    try:
        configured = pwd.getpwuid(manifest.identity.euid).pw_shell
    except (KeyError, OSError):
        raise _SafeFailure(FailureFact(FailurePhase.IDENTITY, FailureCode.ACCOUNT)) from None
    selected = _SHELL_ALIASES.get(configured)
    if selected is None:
        raise _SafeFailure(FailureFact(FailurePhase.IDENTITY, FailureCode.SHELL))
    return configured, selected


def _write_source(source: bytes) -> int:
    try:
        descriptor = os.memfd_create("agentworks-inline-source")
        remaining = memoryview(source)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except (AttributeError, OSError):
        if "descriptor" in locals():
            with suppress(OSError):
                os.close(descriptor)
        raise _SafeFailure(FailureFact(FailurePhase.PREPARE, FailureCode.SOURCE)) from None


def _prepare_launch(manifest: InlineManifest) -> _Launch:
    if manifest.kind is InvocationKind.COMMAND:
        return _Launch(list(manifest.argv), None)
    assert manifest.source is not None
    shell_path, shell = _script_shell(manifest)
    source_fd = _write_source(manifest.source)
    source_path = f"/proc/self/fd/{source_fd}"
    if shell is ScriptShell.BASH:
        return _Launch([shell_path, "--noprofile", "--norc", source_path], source_fd)
    return _Launch([shell_path, source_path], source_fd)


def _retention(mode: OutputMode) -> StreamRetention:
    if mode is OutputMode.CAPTURE:
        return StreamRetention.CAPTURED
    if mode is OutputMode.DISCARD:
        return StreamRetention.DISCARDED
    return StreamRetention.SUPPRESSED


def _emit_stream(
    emitter: _Emitter,
    manifest: InlineManifest,
    name: StreamName,
    result: StreamResult,
) -> None:
    data = result.data if manifest.output_mode is OutputMode.CAPTURE else b""
    retained = data[: manifest.capture_limit]
    truncated = len(data) > manifest.capture_limit
    if retained:
        kind = FrameKind.STDOUT if name is StreamName.STDOUT else FrameKind.STDERR
        emitter.emit(kind, retained)
    emitter.emit(
        FrameKind.STREAM_END,
        encode_stream_end(
            StreamEnd(
                stream=name,
                retained=len(retained),
                sha256=hashlib.sha256(retained).hexdigest() if retained else _EMPTY_SHA256,
                complete=result.complete and not truncated,
                truncated=truncated,
                retention=_retention(manifest.output_mode),
            )
        ),
    )


def _wait_fact(exit_status: int | None) -> WaitFact:
    if exit_status is None:
        return WaitFact(WaitKind.UNKNOWN, None)
    if exit_status < 0:
        return WaitFact(WaitKind.SIGNAL, -exit_status)
    return WaitFact(WaitKind.EXIT, exit_status)


def _process_failure(result: ProcessResult) -> FailureFact | None:
    if result.failure in (None, ProcessFailure.OUTPUT_LIMIT):
        return None
    code = {
        ProcessFailure.INPUT: FailureCode.INPUT,
        ProcessFailure.OUTPUT: FailureCode.OUTPUT,
        ProcessFailure.OBSERVATION: FailureCode.OBSERVATION,
        ProcessFailure.DEADLINE: FailureCode.OBSERVATION,
    }.get(result.failure)
    if code is None:
        return None
    return FailureFact(FailurePhase.OBSERVE, code)


def _finish_failure(emitter: _Emitter, failure: FailureFact) -> int:
    emitter.emit(FrameKind.FAILED, encode_failure(failure))
    emitter.emit(FrameKind.FINISHED, empty_body())
    return 0


def main(nonce: str) -> int:
    """Run one manifest and report only bounded nonce-bound evidence."""
    emitter = _Emitter(nonce)
    try:
        raw_manifest = os.read(0, MAX_MANIFEST_BYTES + 1)
        while len(raw_manifest) <= MAX_MANIFEST_BYTES:
            chunk = os.read(0, MAX_MANIFEST_BYTES + 1 - len(raw_manifest))
            if not chunk:
                break
            raw_manifest += chunk
        manifest = decode_manifest(raw_manifest)
    except ManifestError as error:
        code = FailureCode.OVERSIZED if error.code is ManifestErrorCode.OVERSIZED else FailureCode.INVALID
        return _finish_failure(emitter, FailureFact(FailurePhase.REQUEST, code))
    if manifest.nonce != nonce:
        return _finish_failure(emitter, FailureFact(FailurePhase.REQUEST, FailureCode.NONCE))
    if sys.platform != "linux":
        return _finish_failure(emitter, FailureFact(FailurePhase.PREPARE, FailureCode.RUNTIME))
    if not matches_current_identity(manifest.identity):
        return _finish_failure(emitter, FailureFact(FailurePhase.IDENTITY, FailureCode.MISMATCH))

    try:
        launch = _prepare_launch(manifest)
    except _SafeFailure as error:
        return _finish_failure(emitter, error.fact)

    failure: FailureFact | None = None
    try:
        emitter.emit(FrameKind.LAUNCHING, empty_body())
        result = run_owned_process(
            launch.argv,
            input=ProcessInput(data=manifest.stdin),
            output=(
                ProcessOutput(capture_limit=manifest.capture_limit + 1)
                if manifest.output_mode is OutputMode.CAPTURE
                else ProcessOutput()
            ),
            deadline=Deadline(None),
            env=dict(manifest.env),
            cwd=manifest.cwd,
            pass_fds=() if launch.source_fd is None else (launch.source_fd,),
            start_new_session=True,
        )
        if not result.started:
            failure = FailureFact(FailurePhase.LAUNCH, FailureCode.DISPATCH)
        else:
            _emit_stream(emitter, manifest, StreamName.STDOUT, result.stdout)
            _emit_stream(emitter, manifest, StreamName.STDERR, result.stderr)
            emitter.emit(FrameKind.WAITED, encode_wait(_wait_fact(result.exit_status)))
            failure = _process_failure(result)
    finally:
        if launch.source_fd is not None:
            try:
                os.close(launch.source_fd)
            except OSError:
                if failure is None:
                    failure = FailureFact(FailurePhase.CLEANUP, FailureCode.RESOURCE)
    if failure is not None:
        emitter.emit(FrameKind.FAILED, encode_failure(failure))
    emitter.emit(FrameKind.FINISHED, empty_body())
    return 0
