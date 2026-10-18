"""Destination entry point for the fixed private POSIX inline helper."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from contextlib import ExitStack, suppress
from dataclasses import dataclass

from ._evidence_wire import FrameKind
from ._helper_identity import matches_current_identity, matches_fresh_exec_identity
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
from ._inline_guest_io import FrameWriter, PipeSource, StreamSink, nonblocking_pipe
from ._inline_request import (
    MAX_MANIFEST_BYTES,
    REQUEST_HEADER_BYTES,
    REQUEST_MARKER,
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
    LocalProcessOwner,
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
# Both fixed loaders retain this module through sys.modules. This one-shot
# owner holds uncertain native objects through body return until finalization.
_PROCESS_OWNER = LocalProcessOwner()


class _SafeFailure(Exception):
    def __init__(self, fact: FailureFact) -> None:
        self.fact = fact
        super().__init__(fact.code.value)


class _SourcePipe:
    """Own one Darwin source pipe independently of application input.

    Only the writer thread uses the nonblocking write end. Cancellation does
    not depend on EPIPE: the parent and descendants can retain the read end.
    Failed closure is retained without retrying an ambiguous descriptor close.
    """

    def __init__(self) -> None:
        self.reader: int | None = None
        self._writer: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._writer_closed = threading.Event()
        self._writer_close_attempted = False
        self._reader_close_attempted = False
        self._reader_closed = False

    def start(self, source: bytes) -> int:
        try:
            self.reader, self._writer = os.pipe()
            os.set_blocking(self._writer, False)
            self._thread = threading.Thread(target=self._write, args=(source,), daemon=True)
            self._thread.start()
        except (OSError, RuntimeError):
            raise _SafeFailure(FailureFact(FailurePhase.PREPARE, FailureCode.SOURCE)) from None
        return self.reader

    def _close_writer(self) -> None:
        if self._writer_close_attempted:
            return
        if self._writer is not None:
            self._writer_close_attempted = True
            try:
                os.close(self._writer)
            except OSError:
                return
        self._writer_closed.set()

    def _write(self, source: bytes) -> None:
        assert self._writer is not None
        remaining = memoryview(source)
        try:
            while remaining and not self._stop.is_set():
                try:
                    written = os.write(self._writer, remaining[:65_536])
                except BlockingIOError:
                    self._stop.wait(0.01)
                    continue
                except BrokenPipeError:
                    return
                if written <= 0:
                    raise OSError
                remaining = remaining[written:]
        except OSError:
            self._failed.set()
        finally:
            self._close_writer()

    @property
    def failed(self) -> bool:
        return self._failed.is_set()

    def close(self) -> bool:
        """Cancel and observe closure within 0.5 seconds, retaining uncertainty."""
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(0.5)
            except RuntimeError:
                # A thread whose admission failed has no proved native closure.
                return False
            if self._thread.is_alive():
                return False
        else:
            self._close_writer()
        if self.reader is not None and not self._reader_close_attempted:
            self._reader_close_attempted = True
            try:
                os.close(self.reader)
            except OSError:
                return False
            self._reader_closed = True
        return self._writer_closed.is_set() and (self.reader is None or self._reader_closed)


# Retain actual capabilities even when source cleanup cannot prove closure.
_SOURCE_OWNER = _SourcePipe()


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
    if sys.platform == "darwin":
        source_fd = _SOURCE_OWNER.start(manifest.source)
        source_path = f"/dev/fd/{source_fd}"
    else:
        source_fd = _write_source(manifest.source)
        source_path = f"/proc/self/fd/{source_fd}"
    if shell is ScriptShell.BASH:
        return _Launch([shell_path, "--noprofile", "--norc", source_path], source_fd)
    return _Launch([shell_path, source_path], source_fd)


def _retention(mode: OutputMode) -> StreamRetention:
    if mode is OutputMode.DELIVER:
        return StreamRetention.DELIVERED
    if mode is OutputMode.CAPTURE:
        return StreamRetention.CAPTURED
    if mode is OutputMode.DISCARD:
        return StreamRetention.DISCARDED
    return StreamRetention.SUPPRESSED


def _emit_stream(
    emitter: FrameWriter,
    manifest: InlineManifest,
    name: StreamName,
    result: StreamResult,
    delivered: StreamSink | None = None,
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
                count=len(retained) if delivered is None else delivered.count,
                sha256=(hashlib.sha256(retained).hexdigest() if retained else _EMPTY_SHA256)
                if delivered is None
                else delivered.sha256,
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


def _finish_failure(emitter: FrameWriter, failure: FailureFact) -> int:
    emitter.emit(FrameKind.FAILED, encode_failure(failure))
    emitter.emit(FrameKind.FINISHED, empty_body())
    return 0


def _supports_retrospective_completion() -> bool:
    """Return whether this helper has the audited exact-child wait mechanism."""
    return sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] <= (3, 14)


def _read_exact(length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = os.read(0, length - len(data))
        if not chunk:
            raise ManifestError(ManifestErrorCode.INVALID)
        data.extend(chunk)
    return bytes(data)


def _read_request() -> InlineManifest:
    """Validate an untrusted envelope before any application preparation or launch."""
    header = _read_exact(REQUEST_HEADER_BYTES)
    if not header.startswith(REQUEST_MARKER):
        raise ManifestError(ManifestErrorCode.INVALID)
    length_token = header[len(REQUEST_MARKER) :]
    if any(character not in b"0123456789abcdef" for character in length_token):
        raise ManifestError(ManifestErrorCode.INVALID)
    length = int(length_token, 16)
    if length > MAX_MANIFEST_BYTES:
        raise ManifestError(ManifestErrorCode.OVERSIZED)
    if length == 0:
        raise ManifestError(ManifestErrorCode.INVALID)
    manifest = decode_manifest(_read_exact(length))
    if manifest.stdin is not None and os.read(0, 1):
        raise ManifestError(ManifestErrorCode.INVALID)
    return manifest


def main(nonce: str) -> int:
    """Run one manifest and report only bounded nonce-bound evidence."""
    emitter = FrameWriter(nonce)
    try:
        manifest = _read_request()
    except ManifestError as error:
        code = FailureCode.OVERSIZED if error.code is ManifestErrorCode.OVERSIZED else FailureCode.INVALID
        return _finish_failure(emitter, FailureFact(FailurePhase.REQUEST, code))
    except OSError:
        return _finish_failure(emitter, FailureFact(FailurePhase.REQUEST, FailureCode.INVALID))
    if manifest.nonce != nonce:
        return _finish_failure(emitter, FailureFact(FailurePhase.REQUEST, FailureCode.NONCE))
    if sys.platform not in {"linux", "darwin"} or not _supports_retrospective_completion():
        return _finish_failure(emitter, FailureFact(FailurePhase.PREPARE, FailureCode.RUNTIME))
    matches_identity = matches_fresh_exec_identity if sys.platform == "darwin" else matches_current_identity
    if not matches_identity(manifest.identity):
        return _finish_failure(emitter, FailureFact(FailurePhase.IDENTITY, FailureCode.MISMATCH))

    try:
        launch = _prepare_launch(manifest)
    except _SafeFailure as error:
        if sys.platform == "darwin" and not _SOURCE_OWNER.close():
            emitter.emit(FrameKind.FAILED, encode_failure(error.fact))
            return 1
        return _finish_failure(emitter, error.fact)

    failure: FailureFact | None = None
    source_closed = True
    try:
        emitter.emit(FrameKind.LAUNCHING, empty_body())
        stdout = StreamSink(emitter, StreamName.STDOUT) if manifest.output_mode is OutputMode.DELIVER else None
        stderr = StreamSink(emitter, StreamName.STDERR) if manifest.output_mode is OutputMode.DELIVER else None
        with ExitStack() as pipe_modes:
            if manifest.stdin is None:
                pipe_modes.enter_context(nonblocking_pipe(0))
            if stdout is not None:
                pipe_modes.enter_context(nonblocking_pipe(1))
            result = run_owned_process(
                launch.argv,
                input=ProcessInput(source=PipeSource())
                if manifest.stdin is None
                else ProcessInput(data=manifest.stdin),
                output=(
                    ProcessOutput(stdout_sink=stdout, stderr_sink=stderr)
                    if stdout is not None
                    else ProcessOutput(capture_limit=manifest.capture_limit + 1)
                    if manifest.output_mode is OutputMode.CAPTURE
                    else ProcessOutput()
                ),
                deadline=Deadline(None),
                owner=_PROCESS_OWNER,
                env=dict(manifest.env),
                cwd=manifest.cwd,
                pass_fds=() if launch.source_fd is None else (launch.source_fd,),
                start_new_session=True,
            )
            if stdout is not None and stderr is not None:
                emitter.finish_pending(stdout, stderr)
        if not result.started:
            failure = FailureFact(FailurePhase.LAUNCH, FailureCode.DISPATCH)
        else:
            _emit_stream(emitter, manifest, StreamName.STDOUT, result.stdout, stdout)
            _emit_stream(emitter, manifest, StreamName.STDERR, result.stderr, stderr)
            emitter.emit(FrameKind.WAITED, encode_wait(_wait_fact(result.exit_status)))
            failure = _process_failure(result)
    finally:
        if sys.platform == "darwin" and launch.source_fd is not None:
            source_closed = _SOURCE_OWNER.close()
            if failure is None and not source_closed:
                failure = FailureFact(FailurePhase.CLEANUP, FailureCode.RESOURCE)
            elif failure is None and _SOURCE_OWNER.failed:
                failure = FailureFact(FailurePhase.OBSERVE, FailureCode.INPUT)
        elif launch.source_fd is not None:
            try:
                os.close(launch.source_fd)
            except OSError:
                if failure is None:
                    failure = FailureFact(FailurePhase.CLEANUP, FailureCode.RESOURCE)
    if failure is not None:
        emitter.emit(FrameKind.FAILED, encode_failure(failure))
    if sys.platform == "darwin" and launch.source_fd is not None and not source_closed:
        # No terminal record or successful helper exit while source custody is
        # uncertain. Application stream and wait facts above remain independent.
        return 1
    emitter.emit(FrameKind.FINISHED, empty_body())
    return 0
