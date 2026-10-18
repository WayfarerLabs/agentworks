"""Darwin helper API simulations and source custody on actual POSIX pipes.

These Linux-hosted checks do not establish native macOS acceptance.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.execution import _helper_identity, _inline_guest
from agentworks.execution._evidence_wire import Frame, FrameKind, FrameReader
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._inline_bundle import FIXED_SOURCE
from agentworks.execution._inline_control import (
    FailureCode,
    FailurePhase,
    WaitFact,
    WaitKind,
    parse_failure,
    parse_wait,
)
from agentworks.execution._inline_request import (
    InlineManifest,
    InvocationKind,
    OutputMode,
    ScriptShell,
    encode_request_envelope,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="source descriptors require POSIX")
NONCE = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("mismatch", [None, "uid", "euid", "gid", "egid", "groups"])
def test_fresh_exec_identity_checks_observable_darwin_credentials(
    monkeypatch: pytest.MonkeyPatch, mismatch: str | None
) -> None:
    for name, value in (("uid", 1001), ("euid", 1001), ("gid", 1002), ("egid", 1002)):
        observed = 9999 if mismatch == name else value
        monkeypatch.setattr(os, f"get{name}", lambda observed=observed: observed)
    monkeypatch.setattr(os, "getgroups", lambda: [1004] if mismatch == "groups" else [1003, 1002])
    monkeypatch.delattr(os, "getresuid", raising=False)
    monkeypatch.delattr(os, "getresgid", raising=False)
    expected = IdentityExpectation(1001, 1002, (1002, 1003))
    assert _helper_identity.matches_fresh_exec_identity(expected) is (mismatch is None)


def test_source_pipe_preserves_all_bytes_and_produces_eof() -> None:
    owner = _inline_guest._SourcePipe()
    source = bytes(range(256)) * 4096
    descriptor = owner.start(source)
    collected = bytearray()
    try:
        while chunk := os.read(descriptor, 8192):
            collected.extend(chunk)
        assert collected == source
    finally:
        assert owner.close()
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize("prefix", [0, 7])
def test_source_writer_cancels_with_retained_reader(prefix: int) -> None:
    owner = _inline_guest._SourcePipe()
    descriptor = owner.start(b"x" * 1_048_576)
    retained = os.dup(descriptor)
    if prefix:
        assert os.read(descriptor, prefix) == b"x" * prefix
    started = time.monotonic()
    try:
        assert owner.close()
        assert time.monotonic() - started < 1
        assert owner.close()
        os.fstat(retained)
    finally:
        os.close(retained)


def test_unsettled_writer_retains_reader_until_observed_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _inline_guest._SourcePipe()
    descriptor = owner.start(b"x" * 1_048_576)
    assert owner._thread is not None
    join = owner._thread.join
    alive = owner._thread.is_alive
    monkeypatch.setattr(owner._thread, "join", lambda timeout: None)
    monkeypatch.setattr(owner._thread, "is_alive", lambda: True)
    try:
        assert not owner.close()
        os.fstat(descriptor)
    finally:
        monkeypatch.setattr(owner._thread, "join", join)
        monkeypatch.setattr(owner._thread, "is_alive", alive)
        assert owner.close()


def test_source_write_fault_is_observed_without_hiding_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_write(descriptor: int, data: object) -> int:
        raise OSError

    owner = _inline_guest._SourcePipe()
    monkeypatch.setattr(os, "write", fail_write)
    descriptor = owner.start(b"source")
    try:
        assert os.read(descriptor, 1) == b""
    finally:
        assert owner.close()
    assert owner.failed


def test_writer_close_uncertainty_is_retained_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _inline_guest._SourcePipe()
    original_close = os.close
    attempts: list[int] = []

    def close(selected: int) -> None:
        if selected == owner._writer:
            attempts.append(selected)
            raise OSError
        original_close(selected)

    monkeypatch.setattr(os, "close", close)
    owner.start(b"")
    try:
        assert not owner.close()
        assert not owner.close()
        assert attempts == [owner._writer]
    finally:
        assert owner._writer is not None
        original_close(owner._writer)


def test_source_close_failure_is_retained_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _inline_guest._SourcePipe()
    descriptor = owner.start(b"")
    original_close = os.close
    attempts: list[int] = []

    def close(selected: int) -> None:
        if selected == descriptor:
            attempts.append(selected)
            raise OSError
        original_close(selected)

    monkeypatch.setattr(os, "close", close)
    try:
        assert not owner.close()
        assert not owner.close()
        assert attempts == [descriptor]
    finally:
        original_close(descriptor)


def _manifest(source: bytes, *, stdin: bytes = b"") -> InlineManifest:
    return InlineManifest(
        NONCE,
        InvocationKind.SCRIPT,
        (),
        source,
        ScriptShell.SH,
        (),
        None,
        stdin,
        OutputMode.CAPTURE,
        4096,
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
    )


def _run(manifest: InlineManifest, runtime: str, setup: str = "") -> tuple[int, list[Frame]]:
    # Load subprocess on the actual host first, then remove Linux-only APIs.
    loader = FIXED_SOURCE.rsplit("raise SystemExit", 1)[0]
    probe = """
import os
sys.platform='darwin'
for name in ('getresuid','getresgid','memfd_create'):
 if hasattr(os,name): delattr(os,name)
g=sys.modules['_agw_inline._inline_guest']
"""
    process = subprocess.run(
        [runtime, "-I", "-S", "-B", "-c", loader + probe + setup + "\nraise SystemExit(g.main(sys.argv[1]))", NONCE],
        input=encode_request_envelope(manifest),
        capture_output=True,
        timeout=10,
    )
    assert process.stderr == b""
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)
    pending = memoryview(process.stdout)
    while pending:
        pending = pending[reader.try_write(pending) :]
    reader.finish()
    assert reader.error is None
    return process.returncode, frames


@pytest.fixture(params=[sys.executable, "/usr/bin/python3.11"], ids=["current", "distribution-3.11"])
def runtime(request: pytest.FixtureRequest) -> str:
    selected: str = request.param
    if not Path(selected).is_file():
        pytest.skip("compatibility interpreter unavailable")
    return selected


@pytest.mark.parametrize("shell", [ScriptShell.SH, ScriptShell.BASH])
def test_packed_darwin_simulation_separates_source_input_and_streams(runtime: str, shell: ScriptShell) -> None:
    payload = bytes(range(256)) * 4
    # Exceeds the typical pipe capacity here; no source-size restriction is added.
    source = b"# source\n" * 2200 + b"/bin/cat\nprintf error >&2\nexit 255\n"
    status, frames = _run(replace(_manifest(source, stdin=payload), shell=shell), runtime)
    assert status == 0
    assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDOUT) == payload
    assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDERR) == b"error"
    assert [parse_wait(frame.body) for frame in frames if frame.kind is FrameKind.WAITED] == [
        WaitFact(WaitKind.EXIT, 255)
    ]
    assert not any(frame.kind is FrameKind.FAILED for frame in frames)
    assert frames[-1].kind is FrameKind.FINISHED


def test_packed_darwin_early_exit_does_not_wait_for_all_source(runtime: str) -> None:
    status, frames = _run(_manifest(b"exit 7\n" + b"# padding\n" * 2200), runtime)
    assert status == 0
    assert [parse_wait(frame.body) for frame in frames if frame.kind is FrameKind.WAITED] == [
        WaitFact(WaitKind.EXIT, 7)
    ]
    assert frames[-1].kind is FrameKind.FINISHED


def test_packed_darwin_command_needs_no_script_pipe(runtime: str) -> None:
    manifest = replace(_manifest(b""), kind=InvocationKind.COMMAND, argv=("/bin/true",), source=None, shell=None)
    status, frames = _run(manifest, runtime)
    assert status == 0 and frames[-1].kind is FrameKind.FINISHED
    assert [parse_wait(frame.body) for frame in frames if frame.kind is FrameKind.WAITED] == [
        WaitFact(WaitKind.EXIT, 0)
    ]


@pytest.mark.parametrize("failure", ["source", "dispatch"])
def test_packed_darwin_preparation_and_dispatch_faults_close_source(runtime: str, failure: str) -> None:
    setup = (
        "def fail(): raise OSError\ng.os.pipe=fail\n"
        if failure == "source"
        else "g._script_shell=lambda manifest:('/agentworks-missing-shell',g.ScriptShell.SH)\n"
    )
    status, frames = _run(_manifest(b"exit 0\n" + b"# padding\n" * 2200), runtime, setup)
    assert status == 0
    failures = [parse_failure(frame.body) for frame in frames if frame.kind is FrameKind.FAILED]
    assert len(failures) == 1
    assert failures[0].code is (FailureCode.SOURCE if failure == "source" else FailureCode.DISPATCH)
    assert frames[-1].kind is FrameKind.FINISHED


def test_source_closure_uncertainty_preserves_wait_but_withholds_terminal(runtime: str) -> None:
    setup = """
close=g._SourcePipe.close
def uncertain(self):
 close(self)
 return False
g._SourcePipe.close=uncertain
"""
    status, frames = _run(_manifest(b"printf retained; exit 9\n"), runtime, setup)
    assert status == 1
    assert [parse_wait(frame.body) for frame in frames if frame.kind is FrameKind.WAITED] == [
        WaitFact(WaitKind.EXIT, 9)
    ]
    assert not any(frame.kind is FrameKind.FINISHED for frame in frames)
    failures = [parse_failure(frame.body) for frame in frames if frame.kind is FrameKind.FAILED]
    assert len(failures) == 1
    assert failures[0].phase is FailurePhase.CLEANUP and failures[0].code is FailureCode.RESOURCE
